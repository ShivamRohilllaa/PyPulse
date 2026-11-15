# How the two dashboards compare

The comparison covers the HTTP monitoring, alerting, benchmark and workspace features shipped in the earlier dashboard at revision `910f6b6`. PyPulse implements these workflows in Python with Django, SQLite and a separate worker. Open the [product tour](https://shivamrohilllaa.github.io/PyPulse/) on GitHub Pages. The self-hosted application opens directly on **Traffic** after sign-in.

| Workflow | What PyPulse already supported | What this update adds and where to find it |
| --- | --- | --- |
| Monitor an HTTP endpoint | Scheduled checks, failure thresholds, request preview, tags, environments and network profiles | **Uptime → Add monitor** has a visual editor for headers, query, auth and form or JSON bodies. Accept status classes or ranges and compare nested JSON fields. Preview a request before saving. |
| See what changed | Monitor totals, recent checks, time windows and a dashboard that refreshed periodically | ASGI pushes change events to visible dashboards. **Uptime** groups monitors and shows current latency, failures and 24-hour availability. Monitor details show average and p95 latency, availability history and filtered checks. |
| Respond to an incident | Failure and recovery alerts, acknowledgement, reminders, maintenance and Slack, Teams, SMTP or signed webhooks | **Incidents** distinguishes slow and down incidents, supports manual resolution and shows delivery attempts. Select multiple escalation channels, edit maintenance windows and configure custom webhook headers. |
| Publish service health | Opt-in public monitors and workspace status settings | **Public status** shows up to 90 days of history and active incidents for published monitors. Its public JSON view excludes private targets and request settings. |
| Run a test now or later | Saved tests, interval and daily schedules, release gates, CI tokens and cancellation | **Benchmarks → New benchmark** previews approved targets and estimated work, runs once without saving, or copies an existing test. History records who started new runs and whether they were manual, scheduled or started by CI. Removing the template preserves its reports. |
| Compare client networks | Seven presets, custom profiles, latency distributions and HTTP phase timing | **Latency test** can run profiles together or one at a time. Reports include live progress, latency by profile, per-profile gates, connection reuse, response compression and wire byte counts. |
| Exercise a user journey | Open arrival-rate and closed virtual-user models, five load patterns, multi-step scenarios, fixtures and response extraction | The editor supports up to 20 steps, form fields, dynamic timestamps/UUIDs, and extraction from headers, bracketed JSON paths or bounded regex matches. Each iteration carries its own data. |
| Find a stress limit | Ramps, custom stages, threshold stops and throughput charts | Set independent stop criteria, ramp holds, spike timing and throughput plateau thresholds. The report shows each stage, the breaking point when observed, the highest passing stage, and generator CPU and scheduling delays. |
| Understand or share a result | A/B comparisons, per-request timeline, charts, CSV export and error breakdown | **Run summary** explains measured results and suggests checks for observed failures. Copy or download the text, filter failed or slow samples, inspect the recorded settings and download compressed CSV. Exploratory runs can have a neutral gate. |
| Manage a workspace | Roles, secret variables, CI, import/export, HTTPS, data retention and backups | First-run browser setup, startup configuration import, self-service account changes, searchable audit metadata, masked resource views and session-authenticated HTTP APIs. The standalone image runs both the dashboard and its worker. |
| Observe a Python app in production | Django, DRF, Flask and FastAPI middleware records real traffic, route-level errors, latency histograms and availability. Django also records query counts. | **Traffic** remains the local home screen. Connect service issues a token for one app. Passive collection continues independently of the synthetic tests. |

## Measurement and compatibility details

- One PyPulse worker runs one benchmark job at a time. A latency job can compare multiple profiles concurrently. The earlier dashboard also allowed separate latency jobs to overlap.
- The worker caps traffic at 50 HTTP requests/s, 20 concurrent iterations and 30,000 requests per job. A load profile can run for up to one hour, with a four-hour wall-clock ceiling. The earlier generator accepted higher targets. A result at the PyPulse ceiling describes that worker's measurement range.
- PyPulse applies delay, jitter, loss and rate limits on the client. The earlier engine also modeled TCP retransmission at the segment level. Profile results describe the configured simulation. The HTTP phase breakdown comes from the real transfer.
- Finished HTTP responses contribute to benchmark latency percentiles. Network failures contribute to the error rate. Archived uptime p95 uses a mergeable histogram. Hours archived before histogram support retain availability and averages, and the UI shows percentile coverage.
- A WSGI deployment refreshes dashboard panels by polling. The supplied native launcher and Docker deployments use ASGI for push updates. Private dashboard streams require a session. The opt-in public status page refreshes separately.
- The REST and configuration examples use PyPulse field names, identifiers and token scopes. The native YAML/JSON format and SQLite backup are versioned independently. The [workspace guide](workspace.md) describes the narrower legacy SQLite importer.
- Distributed load generators, multi-region probes, SSO, SMS/phone alerts and additional protocols appeared on the earlier project's roadmap. They were outside its shipped feature set.

## Try the workflows

Start with the [Django demo](../README.md#try-the-django-experiment), then open **Traffic** and look at the route, query count and 5xx columns. Under **Benchmarks**, preview the same endpoint with **Latency** and two profiles. Run once, open the report and compare it with a later release. Configure a synthetic check under **Uptime** if you need a history even when the app has no user traffic.

[Benchmark options and limits](benchmarks.md) · [Monitor rules, alerts and APIs](uptime.md) · [Deployment, CI and workspace APIs](workspace.md)
