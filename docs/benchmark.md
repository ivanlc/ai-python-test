# Verification and benchmark

The original provider, k6 scripts, Grafana dashboard and Compose services are unchanged. All results below come from the final code (`123859c`) on Linux x86-64 with Docker Compose v5.6.0, with fresh containers.

## Checks

| Check | Result |
| --- | --- |
| `ruff check`, `mypy`, `pytest -q` on Python 3.12 and 3.13 (CI: 3.11–3.13) | clean, 216 tests passed |
| `python scripts/smoke.py` against the Compose stack (also in CI) | 20/20 jobs terminal; both colon prompts 5/5 sent; both no-colon prompts 0/5 sent |
| `docker compose run --rm load-test` | 19,243/19,243 checks; 8,247 requests; 0 HTTP failures |
| Real-model evaluation of the shipped prompt (Claude Haiku 5.5) | 268/270 pipeline-correct, 0 incorrect acceptances ([details](llm-evaluation.md)) |

## k6 load test and Grafana scorecard

50 → 150 → 200 virtual users over 40 s, 2,749 iterations. HTTP latency over the three endpoints: mean 0.62 ms, p95 1.13 ms, max 10.8 ms ([console summary](k6-summary.txt)). `/process` only enqueues work, so the API stays fast while the backlog drains.

Scorecard computed with the dashboard's own queries on the run's InfluxDB:

| Panel | Value | Dashboard threshold |
| --- | ---: | --- |
| Global availability / resilience | 100% | green ≥ 99% |
| `/process` mean response | 0.47 ms | |
| Inference attempts per request (`provider_hits` / `/process`) | 2,994 / 2,749 = **1.09** | green < 1.5 |

## Delivery after the queue drained

k6 only checks HTTP statuses. The app was left running until all 2,749 jobs were terminal (about five minutes, bounded by the provider's 5 notifications/s). Counts come from the application and provider logs ([status-snapshot.json](status-snapshot.json)).

| Outcome | Jobs | Explanation |
| --- | ---: | --- |
| `sent` | 1,349 (49%) | 706 email + 643 SMS, each matching one confirmed provider delivery with the full body |
| `failed`: message does not match the body | 1,378 | The two k6 prompts without a colon. The mock always returns the first 50 lowercased characters of the command (`enviar email a juan@example.com diciendo que su pa`). Sending it would be wrong |
| `failed`: refused twice | 22 | The mock refused the first call and the re-ask |
| Unexpected exceptions, breaker openings, deadline expirations | 0 | The mock never fails at transport level |

245 re-asks after a free-text mock refusal recovered all but 22 jobs, so the two extractable prompts were delivered in ≈98% of jobs. 2 explicit `429` responses were absorbed by the shared cooldown.

## Effect of each pipeline version on the same k6 traffic

| Version | Sent | Wrong message sent | AI calls / request |
| --- | ---: | ---: | ---: |
| `ce94604`, before this review (offline simulation of the same mock) | ≈65% | ≈43% of all requests (truncated command) | 1.0 |
| `f4b9185`, grounding fixes | 45% | 0 | 1.0 |
| `c43797f`, re-ask of every model error | 50% | 0 | 1.54 (dashboard warning) |
| `0c391ba`, re-ask of unusable output only | 51% | 0 | 1.09 |
| `123859c`, final | 49% | 0 | 1.09 |

Variations of ±2% in `sent` between runs come from the mock's random refusals and from k6's random choice of prompts. The mock ignores the system prompt, so these runs measure the pipeline, not prompt quality; see the [LLM evaluation](llm-evaluation.md).
