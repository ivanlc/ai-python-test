# Intelligent Notification Service

[![CI](https://github.com/ivanlc/ai-python-test/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanlc/ai-python-test/actions/workflows/ci.yml)

A FastAPI solution for the [AI Engineer Challenge](https://github.com/Lartweib/ai-python-test). It turns a natural-language request into a notification with the bundled AI provider, validates the result against the request, and sends it through the bundled notification provider. The original provider, k6 scenarios, Grafana dashboard, Compose services and license are unchanged.

## For reviewers

| Criterion | What was built | Evidence |
| --- | --- | --- |
| **Robustness to unexpected errors** | Each job is isolated in a fixed worker pool that survives any exception. Requests no answer could satisfy fail before calling the AI. Unusable model output is re-asked once. Circuit breakers fail fast when a provider is down, and a per-job deadline bounds worker time without ever cancelling a send. A notification is retried only after an explicit `429`, never after an ambiguous timeout or `5xx`. | 216 tests, injected failures at every stage. Drained k6 run: all 2,749 jobs terminal, 0 unexpected exceptions, 0 wrong messages sent ([benchmark](docs/benchmark.md)). |
| **Prompt quality** | The prompt lives in [a text file](app/ai/prompts/extraction_system.txt). It defines the exact schema, copies the body verbatim and treats it as data, explains connectors and quotation marks, and uses one `REFUSED` protocol with positive and negative examples. User text never enters the system role. | Shipped prompt with Claude Haiku 5.5: **268/270 pipeline-correct decisions, 0 incorrect acceptances** (the two misses are safe refusals). A local Qwen 3.5 4B run exposed a JSON-injection and a connector issue that are now fixed ([LLM evaluation](docs/llm-evaluation.md)). |
| **Pipeline architecture** | Bounded queue → pre-flight → extraction (format and content guardrails, one re-ask) → rate-limited send → terminal state. The user's text is the source of truth: the guardrails repair only what can be read from it (casing, source connectors, a body split at a later colon, wrapping quotes, a missing `}`) and otherwise fail closed. | Grafana scorecard: 100% availability, 0.47 ms `/process`, 1.09 inference attempts per request ([architecture](docs/architecture.md)). |

**Expected outcome with the mock:** about half of the k6 requests end as `failed`. For the two prompts without a colon, the mock always returns the truncated command (`enviar email a juan@example.com diciendo que su pa`) as the message, and sending it would be wrong. The two other prompts are delivered in ≈98% of jobs.

## API

| Method | Path | Success | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/requests` | `201` | `{"id":"..."}` |
| `POST` | `/v1/requests/{id}/process` | `202` while active, `200` once terminal | `{"id":"...","status":"..."}` |
| `GET` | `/v1/requests/{id}` | `200` | `{"id":"...","status":"queued\|processing\|sent\|failed"}` |

Invalid input returns `422`, unknown IDs `404`, and a full store or queue `503` (the request stays `queued` and can be retried). `202` means accepted, not delivered: poll the `GET` endpoint. Repeated `/process` calls never trigger a second delivery.

Channels: only `email` and `sms`, as the contract defines. A request for another channel (WhatsApp, Telegram, Slack, fax, push) or a channel that conflicts with the recipient fails before calling the AI; it is never silently changed to SMS.

## Run

```sh
docker compose up -d --build provider influxdb grafana app
docker compose run --rm load-test            # k6; scorecard at http://localhost:3000
python scripts/smoke.py                      # optional end-to-end check, as in CI
```

The API listens on `localhost:5000`. The app shares the provider's network namespace (original Compose setup), so it reaches the provider at `http://localhost:3001`. The bundled key `test-dev-2026` is the mock's public test value.

Local development (Python 3.10+, CI tests 3.11–3.13):

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check app evaluation tests && mypy app evaluation && pytest -q
uvicorn app.main:app --port 5000
```

Tests use fakes and HTTPX mock transports; they need neither Docker nor an API key.

## Code layout

```
app/
  main.py               FastAPI entry point (required by the challenge)
  pipeline.py           store, bounded queue, workers, job stages
  ai/prompts/           extraction_system.txt, retry.txt (loaded at startup)
  guardrails/output.py  format stage: parse and losslessly repair model output
  guardrails/grounding.py  content stage: pre-flight and validation against the request
  providers/http.py     exact contracts of /v1/ai/extract and /v1/notify
  providers/resilience.py  rate limiter, circuit breaker, Retry-After
evaluation/             real-model evaluator, datasets and recorded results
scripts/smoke.py        end-to-end check against the Compose stack
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROVIDER_BASE_URL` / `PROVIDER_API_KEY` | `http://localhost:3001` / `test-dev-2026` | Provider origin and key |
| `WORKER_COUNT` / `QUEUE_CAPACITY` / `MAX_RECORDS` | `32` / `4096` / `10000` | Concurrency and memory bounds |
| `RECORD_TTL_SECONDS` | `600` | Age after which terminal records may be pruned at capacity |
| `NOTIFY_LIMIT` / `NOTIFY_WINDOW_SECONDS` | `50` / `10` | Notification rate limit |
| `AI_TIMEOUT_SECONDS` / `NOTIFY_TIMEOUT_SECONDS` | `6` / `3` | Per-call timeouts |
| `AI_RETRIES` / `NOTIFY_429_RETRIES` | `2` / `2` | Transport retries for AI; retries after an explicit `429` |
| `EXTRACTION_RETRIES` | `1` | Re-asks after unusable model output (`0` disables) |
| `JOB_TIMEOUT_SECONDS` | `60` | Maximum worker time per job; never interrupts a send |
| `BREAKER_FAILURE_THRESHOLD` / `BREAKER_COOLDOWN_SECONDS` | `5` / `10` | Circuit breakers for both providers |

## Limitations

Single process, in memory: a restart loses requests, and several app processes would split IDs and rate accounting. Production would need durable state, a broker, distributed throttling and a provider idempotency key. Details in [architecture](docs/architecture.md).

## License

The original [Apache 2.0 license](LICENSE) remains in place.
