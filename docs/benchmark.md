# Verification and benchmark

The original `platform/k6/test.js`, `platform/k6/health-check.js`, Grafana dashboard, provider, and Compose scenarios were not edited. Results below are from executed commands.

## Environment

- Linux x86-64, Docker Compose v5.6.0, stack started with `docker compose up -d --build provider influxdb app` (Grafana is not needed for the measurement).
- Python 3.12 and 3.13 virtual environments for local tests; the container uses the original Python 3.12 base image. CI runs Python 3.11–3.13.
- Earlier runs (revisions `95f45bc` and `f4b9185`) are in the git history of this file.

## Executed checks

| Command | Result |
| --- | --- |
| `pytest -q` (Python 3.12 and 3.13) | 187 passed |
| `ruff check app evaluation tests`, `mypy app evaluation` | clean |
| `python scripts/smoke.py` (Compose stack) | 20/20 jobs terminal; both colon prompts 5/5 sent; both no-colon prompts 0/5 sent |
| `docker compose run --rm load-test` | 19,243/19,243 checks passed; 8,247 HTTP requests; 0 HTTP failures; 2,749 iterations |

### Supplied k6 load test

| Metric | Measured value |
| --- | ---: |
| Ramp | 50 → 150 → 200 target VUs over 40 s |
| Completed iterations | 2,749 |
| HTTP requests | 8,247 (193.4/s) |
| Failed HTTP requests | 0/8,247 (0.00%) |
| Checks | 19,243/19,243 (100%) |
| HTTP duration, mean / median | 0.65 / 0.59 ms |
| HTTP duration, p90 / p95 / max | 0.97 / 1.14 / 11.8 ms |
| Peak active VUs | 199 (200 configured) |

The [captured console summary](k6-summary.txt) is included. `http_req_duration` covers all three API calls; `/process` only enqueues work, so the API stays sub-millisecond while the backlog drains.

The k6 checks accept all four declared states and never wait for delivery, so they measure API availability, not delivery success. Delivery is measured below.

### Processing outcome after the queue drained

The app was left running until every job reached a terminal state (about five minutes, bounded by the provider's 50 notifications per 10 s). Counts come from the application and provider logs and are recorded in [status-snapshot.json](status-snapshot.json).

| Outcome (k6 requests only) | Requests |
| --- | ---: |
| `sent` | 1,374 (50.0%) |
| `failed` | 1,375 (50.0%) |

Including the 20 smoke jobs that ran first on the same container:

| Event | Count | Meaning |
| --- | ---: | --- |
| `failed`: message includes the command | 1,244 | The two k6 prompts without a colon. The mock always answers them with the first 50 lowercased characters of the command (`enviar email a juan@example.com diciendo que su pa`), so the re-ask cannot help |
| `failed`: model refused | 141 | Almost all are no-colon prompts whose re-ask drew a mock refusal; two refusals in a row on a sendable prompt are ~1% |
| Re-asks after a free-text refusal | 278 | The mock's refusals are random, so most were recovered |
| Re-asks after command-text output | 1,225 | Spent on the no-colon prompts; useful with a real model, wasted with this mock |
| AI calls | 4,272 | 1.54 per job |
| Notify HTTP 200 / 429 | 1,384 / 0 | Every `sent` corresponds to one confirmed delivery |
| Unexpected exceptions, breaker openings, deadline expirations | 0 / 0 / 0 | The mock never fails at transport level |

Effect of the re-ask: the colon prompts, which can be extracted correctly, are now delivered in ≈99% of jobs instead of ≈90% (the previous run, without re-ask, sent 1,235 of 2,749). No job was sent with a wrong body. The cost is about 0.5 extra AI calls per job; it does not lower throughput here because notification quota (5/s), not extraction, is the bottleneck.

An offline simulation with the same mock logic (`provider/responses.py` unchanged) confirms the picture: without the guardrail changes of `8eb2bd5`, the no-colon prompts were **sent** in ~86% of draws with the truncated command as the message, and every `cita a las 10:00` request failed.

The mock ignores the system prompt, so these numbers measure the pipeline and guardrails, not prompt quality. Prompt quality is measured with real models in the [LLM evaluation](llm-evaluation.md).

The test stack was stopped after verification.
