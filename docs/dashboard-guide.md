# PyPulse field guide

## Traffic

Sign in and choose **Connect service**, then set its token in the Python workers. Select a time window on **Traffic** to inspect counts and p95 by method and route. Availability measures responses without HTTP 5xx. At a target of 99.9%, one error uses the allowance for 1,000 requests. **Agent configuration → Rotate token** issues a replacement. Update all instances when rotating it. Telemetry is retained for seven days while the worker is running. [Framework setup](../README.md#connect-your-framework)

## Uptime, incidents and notifications

In **Uptime → Add monitor**, enter a URL reachable by the worker. Choose the expected HTTP status, a string to look for in the first 64 KiB of the response, the check interval and how many consecutive failures open an incident. Set a slow threshold in milliseconds. Save and use **Check now**. The detail page records duration, HTTP status and time for each check.

Open **Incidents** to acknowledge a failure. The first passing check records recovery. Set `PYPULSE_ALERT_WEBHOOK` on the worker to receive JSON state changes. If you also set `PYPULSE_ALERT_SECRET`, verify the hexadecimal HMAC-SHA256 of the raw request body against `X-PyPulse-Signature`. Use **Administration → Maintenances** to configure a start and end time for a monitor’s notification silence window. Enable **Public** on an individual monitor to show its name and state at `/status/`.

## Benchmarks

1. Put your **own test host and port** in `PYPULSE_TEST_HOSTS` on both the web process and worker. For a same-machine native install the default includes `127.0.0.1:8000`. Container loopback points back to the container. Use the demo's service name on a shared container network instead.
2. In **Benchmarks → New benchmark**, select your URL and pattern. For the DRF order lookup, use **Fixed**, 10 requests/s and 30 seconds. Save the template and choose **Run**. The worker polls every second.
3. Watch throughput, p95, HTTP statuses, error rate and dropped iterations. Set max p95 and max error rate for a pass criterion. Stop an active test from its report. Download CSV for individual samples or compare two finished runs from the same template.
4. Set **Repeat every N minutes** to schedule the saved benchmark. Set it to zero for manual runs.

| Pattern | Workload |
| --- | --- |
| Fixed / soak | Hold the selected arrival rate. Set a longer duration for a soak. |
| Ramp | Grow from a low rate toward the selected peak. |
| Spike | Introduce a brief peak between two baseline periods. |
| Stress | Ramp to a bounded maximum and stop when a measured threshold is crossed. |
| Latency | Sample one GET per second and inspect client-side end-to-end latency. |

This worker supports GET benchmarks with up to 50 arrivals per second and 20 in flight. Dropped work appears in the result. The traffic report uses histogram bounds. Benchmark percentiles use the measured sample timings.

## CI

Set a high-entropy `PYPULSE_CI_TOKEN` on the web process and save it as a CI secret. Use it for the following two API calls. The first queues an existing test. The second returns its state and `result.passed` after completion.

```sh
curl -fsS -X POST -H "Authorization: Bearer $PYPULSE_CI_TOKEN" "$DASHBOARD/api/ci/tests/$TEST_ID/run"
curl -fsS -H "Authorization: Bearer $PYPULSE_CI_TOKEN" "$DASHBOARD/api/ci/runs/$RUN_ID"
```

## Deploy and back up

Use one worker per installation. The web process and worker share the data directory. The Python dashboard requires Python 3.11+. Only loopback is published by the supplied Compose file. For a domain, use an HTTPS proxy, set `PYPULSE_HOSTS` to the allowed hostname, `PYPULSE_HTTPS=1`, and `PYPULSE_TRUST_PROXY=1` when the proxy overwrites forwarded headers. Keep the web port private behind that proxy.

Use a superuser for initial setup. Signed-in users can read dashboards. Assign Django model permissions via **Administration → Groups** to grant selected write operations. Back up the data volume using a consistent SQLite snapshot or stop both processes before copying. Preserve the signing key along with the database. **Activity** records workspace operations.

To verify a checkout, run `python manage.py check`, `python manage.py test console`, and, after installing `'./python[test]'`, `python -m pytest -q python/tests`.
