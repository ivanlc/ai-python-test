# Verification and benchmark

The original `platform/k6/test.js`, `platform/k6/health-check.js`, Grafana dashboard, provider, and Compose scenarios were not edited. Results below are from executed commands.

## Environment

- Local macOS Docker Desktop 29.5.2, Compose 5.1.4, ARM64.
- Host port 5000 was occupied by macOS Control Center. A temporary, untracked Compose override mapped the provider container's port 5000 to host port 5001; k6 still targeted `http://provider:5000` on the original container network.
- Python 3.9 virtual environment for local tests. Container app uses the original Python 3.12 base image.
- Reviewed application revision: `766e4bc` (2026-10-27).

## Executed checks

| Command | Result |
| --- | --- |
| Local Python 3.9: `pytest -q` | 100 passed in 1.31 s |
| Final application image, Python 3.12: `python -m pytest -q -p no:cacheprovider` | 100 passed in 1.20 s |
| `docker compose run --rm provider-check` | 50/50 checks passed; 20/20 HTTP calls succeeded; mean duration 1.26 s; p95 2.72 s |
| `docker compose run --rm load-test` | 19,243/19,243 checks passed; 8,247 HTTP requests; 0 HTTP failures; 2,749 iterations |

### Supplied k6 load test

| Metric | Measured value |
| --- | ---: |
| Ramp | 50 → 150 → 200 target VUs over 40 s |
| Completed iterations | 2,749 |
| HTTP requests | 8,247 (198.67/s) |
| Failed HTTP requests | 0/8,247 (0.00%) |
| Checks | 19,243/19,243 (100%) |
| HTTP duration, mean / median | 0.870 / 0.654 ms |
| HTTP duration, p90 / p95 / max | 1.72 / 2.25 / 10.26 ms |
| Peak active VUs | 199 (200 configured) |

The k6 container exited with code 0. The [captured console summary](k6-summary.txt) is included for audit. Its `http_req_duration` covers all three API calls, not only `/process`; the supplied Grafana dashboard can display per-endpoint time series from InfluxDB.

The k6 load test measures API acceptance and status retrieval over a 40-second ramp to 200 virtual users. Its status check accepts all four declared states; it does not wait for delivery. Results therefore describe API responsiveness and availability, not delivery throughput or success rate. The simulated AI takes 1.5–3 seconds and the notification provider caps accepted attempts at 50 per 10 seconds. A backlog continuing after the k6 run is expected; the bounded queue eventually returns `503` under sustained overload.

### Observed processing after load

All 2,749 created IDs were queried starting at `2026-10-27T13:01:46Z`, over 4.658 seconds. This is a non-atomic sample while workers continued draining, not a final delivery success rate. The [captured sample](status-snapshot.json) contains:

| State | Sampled records |
| --- | ---: |
| `sent` | 525 |
| `failed` | 273 |
| `processing` | 32 |
| `queued` | 1,919 |

Provider logs captured at the end of that sample contained 543 successful notification responses and one explicit `429` response. The difference from sampled `sent` records is expected because processing continued during the queries. At the end of sampling, application warning logs contained 114 incomplete/refused-output failures and 170 rejections of partial explicit bodies. Those warning totals are also collected after the state queries and are not an atomic count of the sampled `failed` state. The latter guardrail prevents the mock from sending only `00` for a request whose full body is `cita a las 10:00`; see the [quality criteria review](quality-review.md).

The limiter controls local reservation times, while the provider counts arrival times, so scheduling and transport can still produce boundary rate-limit responses. The shared cooldown handles explicit rejections without retrying uncertain notification outcomes. These measurements validate pipeline behavior with the supplied mock; its system-prompt quality cannot be measured because the mock ignores those instructions.

The test stack was stopped after verification. Any remaining in-memory benchmark jobs were discarded at shutdown, as documented in the architecture limitations.
