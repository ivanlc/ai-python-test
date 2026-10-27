# Verification and benchmark

The original `platform/k6/test.js`, `platform/k6/health-check.js`, Grafana dashboard, provider, and Compose scenarios were not edited. Results below are from executed commands.

## Environment

- Local macOS Docker Desktop 29.5.2, Compose 5.1.4, ARM64.
- Host port 5000 was occupied by macOS Control Center. A temporary, untracked Compose override mapped the provider container's port 5000 to host port 5001; k6 still targeted `http://provider:5000` on the original container network.
- Python 3.9 virtual environment for local tests. Container app uses the original Python 3.12 base image.
- Reviewed application revision: `62c003b` (2026-10-27).

## Executed checks

| Command | Result |
| --- | --- |
| Local Python 3.9: `pytest -q` | 86 passed in 1.22 s |
| Final application image, Python 3.12: `python -m pytest -q -p no:cacheprovider` | 86 passed in 1.15 s |
| `docker compose run --rm provider-check` | 50/50 checks passed; 20/20 HTTP calls succeeded; mean duration 1.26 s; p95 2.72 s |
| `docker compose run --rm load-test` | 19,187/19,187 checks passed; 8,223 HTTP requests; 0 HTTP failures; 2,741 iterations |

### Supplied k6 load test

| Metric | Measured value |
| --- | ---: |
| Ramp | 50 → 150 → 200 target VUs over 40 s |
| Completed iterations | 2,741 |
| HTTP requests | 8,223 (198.35/s) |
| Failed HTTP requests | 0/8,223 (0.00%) |
| Checks | 19,187/19,187 (100%) |
| HTTP duration, mean / median | 0.913 / 0.691 ms |
| HTTP duration, p90 / p95 / max | 1.81 / 2.36 / 12.00 ms |
| Peak active VUs | 199 (200 configured) |

The k6 container exited with code 0. The [captured console summary](k6-summary.txt) is included for audit. Its `http_req_duration` covers all three API calls, not only `/process`; the supplied Grafana dashboard can display per-endpoint time series from InfluxDB.

The k6 load test measures API acceptance and status retrieval over a 40-second ramp to 200 virtual users. Its status check accepts all four declared states; it does not wait for delivery. Results therefore describe API responsiveness and availability, not delivery throughput or success rate. The simulated AI takes 1.5–3 seconds and the notification provider caps accepted attempts at 50 per 10 seconds. A backlog continuing after the k6 run is expected; the bounded queue eventually returns `503` under sustained overload.

### Observed processing after load

All 2,741 created IDs were queried starting at `2026-10-27T12:45:33Z`, over 4.818 seconds. This is a non-atomic sample while workers continued draining, not a final delivery success rate. The [captured sample](status-snapshot.json) contains:

| State | Sampled records |
| --- | ---: |
| `sent` | 379 |
| `failed` | 61 |
| `processing` | 32 |
| `queued` | 2,269 |

Provider logs captured at the end of that sample contained 379 successful notification responses and two explicit `429` responses. Application failure logs observed during this review reported incomplete/refused extraction output; no terminal notification failure was observed. The limiter controls local reservation times, while the provider counts arrival times, so scheduling and transport can still produce boundary rate-limit responses. The shared cooldown handles these explicit rejections without retrying uncertain notification outcomes.

The test stack was stopped after verification. Any remaining in-memory benchmark jobs were discarded at shutdown, as documented in the architecture limitations.
