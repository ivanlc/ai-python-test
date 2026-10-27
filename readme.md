# Intelligent Notification Service

A FastAPI solution for the [AI Engineer Challenge](https://github.com/Lartweib/ai-python-test). It accepts a natural-language request, extracts a notification through the bundled mock AI provider, validates the result, and sends it through the bundled notification provider. The original provider, k6 scenarios, Grafana dashboard, Compose services, and license remain intact.

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

For local Python development (Python 3.9+):

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --host 0.0.0.0 --port 5000
```

The provider must be reachable at `PROVIDER_BASE_URL` for end-to-end processing. Tests use a fake provider and HTTPX mock transport, so they do not require Docker.

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

## Design and limitations

`app/main.py` handles HTTP, `app/service.py` owns the state machine and queue, `app/parsing.py` parses and validates model output, `app/models.py` defines public and domain models, and `app/provider.py` implements the exact external contracts with one reusable HTTPX client. See [architecture](docs/architecture.md) for guardrails, retries, rate limiting, and trade-offs.

This is a **single-process, in-memory** implementation. A restart loses IDs and pending work; jobs interrupted after provider acceptance may have an unknown outcome. The queue, rate limiter, and idempotency guard are not shared across replicas or multiple Uvicorn workers. Production would require durable state, a broker, distributed throttling, and a provider-supported idempotency key. Queue and record caps apply backpressure instead of allowing unbounded memory growth.

## Verification

`pytest -q` covers HTTP contracts and OpenAPI, transitions, concurrent idempotency, simulator variants, unsafe/ambiguous outputs, invalid recipients, provider failures and cooldowns, configuration, concurrency, and overload. The original k6 scripts are untouched. Recorded results and environment notes are in [benchmark](docs/benchmark.md).

The [quality criteria review](docs/quality-review.md) explains the prompt design, failure isolation, pipeline trade-offs, and the limits of evaluating prompt quality with a simulator that ignores system instructions.

An optional [real-model evaluator](evaluation/README.md) compares prompts against local Ollama without sending notifications. The [executed Qwen evaluation](docs/llm-evaluation.md) includes 100 scored real-model calls, original failures, production guardrail fixes, and unchanged-output re-scoring.

## License

The original [Apache 2.0 license](LICENSE) remains in place.
