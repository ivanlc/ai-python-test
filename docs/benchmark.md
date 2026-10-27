# Verification and benchmark

The original `platform/k6/test.js`, `platform/k6/health-check.js`, Grafana dashboard, provider, and Compose scenarios were not edited. Results below are from executed commands.

## Environment

- Local macOS Docker Desktop 29.5.2, Compose 5.1.4, ARM64.
- Host port 5000 was occupied by macOS Control Center. A temporary, untracked Compose override mapped the provider container's port 5000 to host port 5001; k6 still targeted `http://provider:5000` on the original container network.
- Python 3.9 virtual environment for local tests. Container app uses the original Python 3.12 base image.

## Executed checks

| Command | Result |
| --- | --- |
| `pytest -q` | 37 passed in 0.79 s |
| `docker compose run --rm provider-check` | 50/50 checks passed; 20/20 HTTP calls succeeded; mean duration 1.26 s; p95 2.72 s |
| `docker compose run --rm load-test` | 19,243/19,243 checks passed; 8,247 HTTP requests; 0 HTTP failures; 2,749 iterations |

### Supplied k6 load test

| Metric | Measured value |
| --- | ---: |
| Ramp | 50 → 150 → 200 target VUs over 40 s |
| Completed iterations | 2,749 |
| HTTP requests | 8,247 (198.78/s) |
| Failed HTTP requests | 0/8,247 (0.00%) |
| Checks | 19,243/19,243 (100%) |
| HTTP duration, mean / median | 0.684 / 0.561 ms |
| HTTP duration, p90 / p95 / max | 1.22 / 1.52 / 9.28 ms |
| Peak active VUs | 199 (200 configured) |

The k6 container exited with code 0. The [captured console summary](k6-summary.txt) is included for audit. Its `http_req_duration` covers all three API calls, not only `/process`; the supplied Grafana dashboard can display per-endpoint time series from InfluxDB.

The k6 load test measures API acceptance and status retrieval over a 40-second ramp to 200 virtual users. Its status check accepts all four declared states; it does not wait for delivery. Results therefore describe API responsiveness and availability, not delivery throughput or success rate. The simulated AI takes 1.5–3 seconds and the notification provider caps accepted attempts at 50 per 10 seconds. A backlog continuing after the k6 run is expected; the bounded queue eventually returns `503` under sustained overload.
