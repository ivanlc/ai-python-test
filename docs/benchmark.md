# Verification and benchmark

The original `platform/k6/test.js`, `platform/k6/health-check.js`, Grafana dashboard, provider, and Compose scenarios were not edited. Results below are from executed commands.

## Environment

- Linux x86-64, Docker Compose v5.6.0, stack started with `docker compose up -d --build provider influxdb app` (Grafana is not needed for the measurement).
- Python 3.12 virtual environment for local tests; the container uses the original Python 3.12 base image.
- An earlier run of revision `95f45bc` on macOS/ARM64 is in the git history of this file.

## Executed checks

| Command | Result |
| --- | --- |
| `pytest -q` (Python 3.12) | 161 passed |
| `docker compose run --rm load-test` | 19,243/19,243 checks passed; 8,247 HTTP requests; 0 HTTP failures; 2,749 iterations |

### Supplied k6 load test

| Metric | Measured value |
| --- | ---: |
| Ramp | 50 → 150 → 200 target VUs over 40 s |
| Completed iterations | 2,749 |
| HTTP requests | 8,247 (182.9/s) |
| Failed HTTP requests | 0/8,247 (0.00%) |
| Checks | 19,243/19,243 (100%) |
| HTTP duration, mean / median | 1.08 / 0.79 ms |
| HTTP duration, p90 / p95 / max | 1.29 / 1.56 ms / 1.78 s |
| Peak active VUs | 199 (200 configured) |

The [captured console summary](k6-summary.txt) is included. `http_req_duration` covers all three API calls. The single 1.78 s maximum is an outlier; p95 stays below 2 ms because `/process` only enqueues work.

The k6 checks accept all four declared states and never wait for delivery, so they measure API availability, not delivery success. Delivery is measured below.

### Processing outcome after the queue drained

After the run the app was left running until all 2,749 jobs reached a terminal state (about four minutes, bounded by the provider's 50 notifications per 10 s). Counts come from the application and provider logs and are recorded in [status-snapshot.json](status-snapshot.json).

| Outcome | Requests | Cause |
| --- | ---: | --- |
| `sent` | 1,235 (45%) | 621 email + 614 SMS; equals the provider's 1,235 HTTP 200 responses |
| `failed`: message includes the command | 1,241 (45%) | The two k6 prompts without a colon. The mock returns the first 50 lowercased characters of the command (`enviar email a juan@example.com diciendo que su pa`), which is rejected |
| `failed`: model refused | 273 (10%) | The mock's plain-text refusals |
| Unexpected exceptions | 0 | |

19 explicit `429` responses were absorbed by the shared cooldown and retried; none caused a failure. Every truncated object and every output with a missing `to` or `type` was recovered. All `cita a las 10:00` requests were sent with their full body instead of the mock's `00`, and `Tienes un nuevo mensaje.` keeps its capital letter.

An offline simulation with the same mock logic (4,000 draws per k6 prompt, `provider/responses.py` unchanged) gives the same picture: the colon prompts are sent with the exact body in ~90% of draws (the rest are refusals), the prompts without a colon are never sent with a wrong message. With the previous guardrails (`95f45bc`) these two prompts were **sent** in ~86% of draws with the truncated command as the message, and every `cita a las 10:00` request failed.

The mock ignores the system prompt, so these numbers measure the pipeline and guardrails, not prompt quality. Prompt quality is measured with real models in the [LLM evaluation](llm-evaluation.md); there Claude Haiku 5.5 extracts all four k6 prompts correctly.

The test stack was stopped after verification.
