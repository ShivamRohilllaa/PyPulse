# PyPulse field guide

## Traffic

Open the application root to go directly to **Traffic**. The public product tour lives on [GitHub Pages](https://shivamrohilllaa.github.io/PyPulse/).

Sign in and choose **Connect service**, then set its token in the Python workers. Select a time window on **Traffic** to inspect counts and p95 by method and route. Availability measures responses without HTTP 5xx. At a target of 99.9%, one error uses the allowance for 1,000 requests. **Agent configuration → Rotate token** issues a replacement. Update all instances when rotating it. The default telemetry retention is seven days. Workspace administrators can adjust it. [Framework setup](../README.md#connect-your-framework)

## Uptime, incidents and notifications

Open **Uptime → Add monitor**. For the included Django app, choose `GET http://127.0.0.1:8000/orders/42`, expect HTTP 200 and match the JSON field `state` to `ready`. Set a check interval, a slow threshold and the number of failures that should open an incident. Save and choose **Check now**. The detail screen shows response times, uptime, recent checks and incidents for the selected window.

Use **Incidents → Alert channels** for Slack, Teams, SMTP email or signed webhooks. Choose the channels and alert events for each monitor. Schedule maintenance from its detail page, or mark it public to include it on the workspace status page. [Uptime, alerts and recovery](uptime.md)

## Benchmarks

1. Add the host you operate under **Workspace → Workspace settings → Benchmark targets**, or set `PYPULSE_TEST_HOSTS`. A same-host install includes `127.0.0.1:8000`. In Compose, use the demo service’s hostname on a shared network.
2. Try **Benchmarks → New benchmark** with the local order lookup. Choose **Fixed**, 10 iterations per second, one request per iteration and 30 seconds. Save and choose **Run**. Follow throughput, tail latency, HTTP status codes, errors and drops.
3. Select **Latency** and two network profiles to compare transport timing. Configure a multi-step journey with CSV inputs and extract an ID or header from the first response for use in the next request.
4. **Preview workload** shows hosts and estimated requests. **Run once** measures a draft without creating a schedule. Open **Run summary** for measured findings, suggested checks and a copyable text report.
5. Download samples from the report. **Compare two runs** shows how throughput, p95 and errors changed. Set pass thresholds and schedule saved tests by interval or time of day. [Benchmark setup, patterns and limits](benchmarks.md)

## Network profiles and variables

Open **Workspace → Network profiles** to inspect the seven presets or save a custom profile. Set upload/download in kbit/s, a delay in milliseconds, optional jitter and the percentage of requests to drop. Applying the profile affects requests made by the PyPulse worker.

Open **Workspace → Variables & secrets**. Add `BASE_URL` with the environment `staging` and set its value to your staging API. In a monitor or test with the same environment, request `{{BASE_URL}}/orders/42`. A scoped value overrides the global variable of the same name. Secret values stay masked on the dashboard. Back up the dashboard signing key with the database.

## CI

Create a scoped CI token under **Workspace → CI tokens**. Start a saved test through `POST /api/ci/tests/<id>/run` and poll `GET /api/ci/runs/<id>`. A release gate should inspect the finished run’s pass result. [CI example and token scopes](workspace.md#ci-performance-gate)

## Deploy and back up

Run the Django web service and a single scheduler/benchmark worker against the same data volume. For local use, the supplied Compose file binds the dashboard to loopback. The optional Caddy overlay serves a real hostname with HTTPS.

Use **Workspace** to manage roles, configure retention, transfer reusable configuration as YAML/JSON and enable the public status page. An installation backup includes the SQLite database, signing key and deployment environment. [Deployment and backup commands](workspace.md)

Verify a checkout with `python manage.py check`, `python manage.py test console` and, after installing `'./python[test]'`, `python -m pytest -q python/tests`.
