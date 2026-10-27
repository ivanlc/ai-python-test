# Verification and benchmark

The original provider, k6 scripts, Grafana dashboard and Compose services are unchanged. All numbers come from executed runs on Linux x86-64 with Docker Compose v5.6.0, revision `0c391ba`.

## Checks

| Check | Result |
| --- | --- |
| `ruff`, `mypy`, `pytest -q` on Python 3.11–3.13 (CI) | clean, 193 tests passed |
| `python scripts/smoke.py` against the Compose stack (CI) | every job terminal; colon prompts sent, no-colon prompts never sent |
| `docker compose run --rm load-test` | 19,243/19,243 checks; 8,247 requests; 0 HTTP failures |

## k6 load test

50 → 150 → 200 virtual users over 40 s, 2,749 iterations. HTTP latency over all three endpoints: mean 0.65 ms, p95 1.16 ms, max 6.7 ms ([console summary](k6-summary.txt)). `/process` only enqueues, so the API stays fast while the backlog drains.

The Grafana scorecard, computed with the dashboard's own queries on the run's InfluxDB:

| Panel | Value | Dashboard threshold |
| --- | ---: | --- |
| Global availability / resilience | 100% | green ≥ 99% |
| `/process` mean response | 0.47 ms | |
| Inference attempts per request (`provider_hits` / `/process`) | 2,999 / 2,749 = **1.09** | green < 1.5 |

## Delivery after the queue drained

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
