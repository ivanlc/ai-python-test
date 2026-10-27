# Intelligent Notification Service

[![CI](https://github.com/ivanlc/ai-python-test/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanlc/ai-python-test/actions/workflows/ci.yml)

A FastAPI solution for the [AI Engineer Challenge](https://github.com/Lartweib/ai-python-test). It accepts a natural-language request, extracts a notification through the bundled mock AI provider, validates the result, and sends it through the bundled notification provider. The original provider, k6 scenarios, Grafana dashboard, Compose services, and license remain intact.

## For reviewers: the three evaluation criteria

| Criterion | What was built | Evidence |
| --- | --- | --- |
| **Robustness to unexpected errors** | Every job is isolated in a fixed worker pool; workers survive any exception. Pre-flight rejects impossible requests without an AI call. Off-protocol model output gets one re-ask. Circuit breakers fail fast when a provider is down; a per-job deadline bounds worker time without ever cancelling a send. Notifications are never retried after an ambiguous outcome (timeout, `5xx`), only after an explicit `429`. | 187 tests incl. injected failures, breaker and deadline tests; drained k6 run: 0 unexpected exceptions, 0 wrong messages sent ([benchmark](docs/benchmark.md)) |
| **Prompt quality** | Exact schema, body copied verbatim and treated as data, connector and quotation rules, one `REFUSED` protocol, positive and negative examples. User text is never placed in the system role. | Real-model evaluation: Claude Haiku 5.5 reaches 119/120, 60/60, 30/30 on earlier sets and 54/60 raw on a set frozen before the prompt was run, with **0 incorrect acceptances under the final guardrails** across 1,263 Haiku and 100 Qwen calls ([LLM evaluation](docs/llm-evaluation.md)) |
| **Pipeline architecture** | Bounded queue → pre-flight → extraction with guardrails and re-ask → rate-limited send → terminal state. Guardrails treat the user's text as the source of truth: they repair only what can be read from it (casing, a body split at a later colon, wrapping quotes, a missing `}`) and otherwise fail closed. | Mock outputs that are wrong but grounded (`enviar email a juan@... diciendo que su pa`, `00`) are rejected or repaired; colon prompts are delivered in ≈99% of jobs ([architecture](docs/architecture.md), [quality review](docs/quality-review.md)) |

With the bundled mock, half of the k6 requests end as `failed` **by design**: for the two prompts without a colon the mock always returns the truncated command as the message, and sending it would be wrong. k6 itself only checks HTTP statuses and passes 100%.

## API

| Method | Path | Success | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/requests` | `201` | `{"id":"..."}` |
| `POST` | `/v1/requests/{id}/process` | `202` while active; `200` after completion | `{"id":"...","status":"..."}` |
| `GET` | `/v1/requests/{id}` | `200` | `{"id":"...","status":"queued\|processing\|sent\|failed"}` |

Create accepts `{"user_input":"Send an email to user@example.com: Hello"}`. Invalid input returns `422`, unknown IDs return `404`, and a full request store or queue returns `503`. A `503` from `/process` leaves the request `queued`, so the same ID can be retried. Repeated `/process` calls for an accepted or completed ID do not trigger another delivery.

A `202` confirms queue acceptance, not delivery. Poll `GET /v1/requests/{id}` for `sent` or `failed`. A terminal `failed` is not automatically retried.

## Run

Prerequisites: Docker Desktop and Compose. From the repository root:

```sh
docker compose up -d --build provider influxdb grafana app
docker compose run --rm provider-check
docker compose run --rm load-test
```

The API listens on `localhost:5000`, the provider on `localhost:3001`, and Grafana on `localhost:3000`. The original Compose setup shares the provider's network namespace with the app, so its default provider URL is `http://localhost:3001` inside the containers. The bundled mock API key is the public test value `test-dev-2026`; set `PROVIDER_API_KEY` for another environment. No production credential is stored here.

For local Python development (Python 3.10+; CI tests 3.11–3.13):

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ruff check app evaluation tests && mypy app evaluation && pytest -q
uvicorn app.main:app --host 0.0.0.0 --port 5000
```

The provider must be reachable at `PROVIDER_BASE_URL` for end-to-end processing. Tests use a fake provider and HTTPX mock transport, so they do not require Docker. With the Compose stack running, `python scripts/smoke.py` sends the k6 prompts through the API and checks their terminal states, as CI does.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROVIDER_BASE_URL` | `http://localhost:3001` | Shared AI and notification origin |
| `PROVIDER_API_KEY` | `test-dev-2026` | Bundled mock provider key |
| `WORKER_COUNT` | `32` | Concurrent processing jobs |
| `QUEUE_CAPACITY` | `4096` | Accepted jobs waiting for a worker |
| `MAX_RECORDS` | `10000` | In-memory request cap |
| `RECORD_TTL_SECONDS` | `600` | Minimum age before terminal or unstarted records can be pruned at capacity |
| `NOTIFY_LIMIT` | `50` | Notification attempts per rolling window |
| `NOTIFY_WINDOW_SECONDS` | `10` | Rolling window length |
| `AI_TIMEOUT_SECONDS` | `6` | Timeout for one AI call |
| `NOTIFY_TIMEOUT_SECONDS` | `3` | Timeout for one notification call |
| `AI_RETRIES` | `2` | Additional safe AI attempts |
| `NOTIFY_429_RETRIES` | `2` | Additional notification attempts after explicit `429` |
| `EXTRACTION_RETRIES` | `1` | Re-asks after off-protocol model output (`0` disables) |
| `JOB_TIMEOUT_SECONDS` | `60` | Maximum worker time per job; never interrupts a send |
| `BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive provider failures that open a circuit |
| `BREAKER_COOLDOWN_SECONDS` | `10` | Time an open circuit fails fast before one probe |

## Design and limitations

`app/main.py` handles HTTP, `app/service.py` owns the state machine and queue, `app/parsing.py` parses and validates model output, `app/models.py` defines public and domain models, and `app/provider.py` implements the exact external contracts with one reusable HTTPX client. See [architecture](docs/architecture.md) for the job stages, guardrails, retries, breakers, rate limiting, and trade-offs.

This is a **single-process, in-memory** implementation. A restart loses IDs and pending work; jobs interrupted after provider acceptance may have an unknown outcome. The queue, rate limiter, and idempotency guard are not shared across replicas or multiple Uvicorn workers. Production would require durable state, a broker, distributed throttling, and a provider-supported idempotency key. Queue and record caps apply backpressure instead of allowing unbounded memory growth.

## Verification

`pytest -q` (187 tests) covers HTTP contracts and OpenAPI, transitions, concurrent idempotency, worker survival, pre-flight, re-ask, job deadlines, circuit breakers, simulator variants including the mock's truncated and command-text outputs, lossless repairs, unsafe/ambiguous outputs, invalid recipients, provider failures and cooldowns, configuration, concurrency, overload, and the evaluator. The original k6 scripts are untouched. Recorded results, including the delivery outcome of every k6 request after the queue drained, are in [benchmark](docs/benchmark.md).

The [quality criteria review](docs/quality-review.md) explains the prompt design, failure isolation, pipeline trade-offs, and the limits of evaluating prompt quality with a simulator that ignores system instructions.

An optional [real-model evaluator](evaluation/README.md) compares prompts against local Ollama or the Anthropic API without sending notifications. The [executed evaluation](docs/llm-evaluation.md) covers 100 Qwen 3.5 4B calls and 1,263 Claude Haiku 5.5 calls (including a re-ask run), including the k6 prompts and a set frozen before the current prompt was tested, with original failures, guardrail fixes and unchanged-output re-scoring.

## License

The original [Apache 2.0 license](LICENSE) remains in place.
