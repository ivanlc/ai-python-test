# Verification and benchmark

The original provider, k6 scripts, Grafana dashboard and Compose services are unchanged. The final local verification is below; the earlier Linux comparison is retained separately.

## Final local verification, 2026-10-27

Application commit `ea145c8`, macOS ARM64, Python 3.12.14, Docker Desktop. Local Ollama inference ran concurrently with the load test. The raw [k6 summary](delivery/k6-summary.json), [console](delivery/k6-console.txt), [host checks](delivery/host-checks.txt), and [container checks](delivery/container-checks.txt) are committed.

| Check | Executed result |
| --- | --- |
| `pytest -q` on host and inside the application image | 213 passed in each environment |
| `ruff check app evaluation tests`, `mypy app evaluation` | clean on host and in the image |
| Final Compose smoke | 20/20 jobs terminal; all 10 extractable jobs sent, no truncated-command job sent; [output](delivery/smoke.txt) |
| Original k6 stages, 50 → 150 → 200 users | 2,749 iterations; 8,247 HTTP requests; 19,243/19,243 checks passed |
| HTTP failures | 0/8,247 |
| HTTP duration | mean 0.798 ms; p95 1.906 ms; max 5.863 ms |
| Real local Qwen extraction evaluation | 267/270 pipeline-correct decisions, 0 incorrect acceptances; [details](llm-evaluation.md) |

All **2,749 jobs** reached a terminal state. Each status was then checked through the public API and 20 terminal `/process` calls returned the idempotent `200`. The [snapshot](delivery/status-snapshot.json) records the application file hashes, log hashes and Grafana queries' results after draining:

| Outcome / scorecard | Value |
| --- | ---: |
| `sent`, confirmed by the same number of provider `200`s | 1,371 (694 email, 677 SMS) |
| `failed`, mock message does not match the complete source body | 1,345 |
| `failed`, model refusal after the available attempts | 33 |
| Unexpected exceptions / breaker openings / deadline expirations | 0 / 0 / 0 |
| Notification `429`s handled by shared cooldown | 24 |
| Availability | 100% |
| `/process` mean duration | 0.576 ms |
| Inference attempts per request | 3,011 / 2,749 = 1.095 |

The two no-colon k6 prompts produce a truncated command in the mock, so their rejection is expected. Delivery is not equivalent to HTTP availability. The live snapshot verifies terminal states and confirmation counts; exact payload correctness is covered by guardrail tests and the separate real-model scorer, not by the original k6 assertions.

The host's port 5000 was occupied by macOS. A temporary Compose override outside the repository mapped **host 5001 → container 5000** for the smoke and status checks. The application's port remained 5000, k6 kept `TARGET_URL=http://provider:5000`, and none of the evaluation scenarios were edited. Summary export only added `--summary-export=/results/k6-summary.json` to the original k6 invocation. The `delivery/pre-connector/` files retain an earlier HTTP-only run before the final body-boundary correction; they are not the final delivery evidence.

## Historical Linux run (`0c391ba`)

The following figures were measured on Linux x86-64 with Docker Compose v5.6.0 and are retained for the implementation comparison.

### Checks

| Check | Result |
| --- | --- |
| `ruff`, `mypy`, `pytest -q` on Python 3.11–3.13 (CI) | clean, 193 tests passed |
| `python scripts/smoke.py` against the Compose stack (CI) | every job terminal; colon prompts sent, no-colon prompts never sent |
| `docker compose run --rm load-test` | 19,243/19,243 checks; 8,247 requests; 0 HTTP failures |

### k6 load test

50 → 150 → 200 virtual users over 40 s, 2,749 iterations. HTTP latency over all three endpoints: mean 0.65 ms, p95 1.16 ms, max 6.7 ms ([console summary](k6-summary.txt)). `/process` only enqueues, so the API stays fast while the backlog drains.

The Grafana scorecard, computed with the dashboard's own queries on the run's InfluxDB:

| Panel | Value | Dashboard threshold |
| --- | ---: | --- |
| Global availability / resilience | 100% | green ≥ 99% |
| `/process` mean response | 0.47 ms | |
| Inference attempts per request (`provider_hits` / `/process`) | 2,999 / 2,749 = **1.09** | green < 1.5 |

### Delivery after the queue drained

k6 only checks HTTP statuses. The app was left running until all 2,749 jobs were terminal (about five minutes, bounded by the provider's 5 notifications/s); counts from application and provider logs are in [status-snapshot.json](status-snapshot.json).

| Outcome | Jobs | Explanation |
| --- | ---: | --- |
| `sent` | 1,409 (51%) | Each matches one confirmed provider delivery with the correct body |
| `failed`, message includes the command | 1,321 | The two k6 prompts without a colon: the mock always returns the first 50 lowercased characters of the command. Sending it would be wrong |
| `failed`, refused twice | 19 | The mock refused the first call and the re-ask |
| Unexpected exceptions, breaker openings, deadline expirations | 0 | The mock never fails at transport level |

250 re-asks after a free-text mock refusal recovered all but 19 jobs, so the two extractable prompts were delivered in ≈99% of jobs. 19 explicit `429` responses were absorbed by the shared cooldown.

How the guardrails and the re-ask changed this, for the same k6 traffic:

| Version | Sent | Wrong message sent | AI calls / request |
| --- | ---: | ---: | ---: |
| `ce94604` (before this review, offline simulation of the same mock) | ≈65% | ≈43% of all requests (truncated command) | 1.0 |
| `f4b9185` grounding fixes | 45% | 0 | 1.0 |
| `c43797f` re-ask of every model error | 50% | 0 | 1.54 (dashboard warning) |
| `0c391ba` re-ask of unusable output only | 51% | 0 | 1.09 |

The mock ignores the system prompt, so these runs measure the pipeline, not prompt quality; see the [LLM evaluation](llm-evaluation.md).
