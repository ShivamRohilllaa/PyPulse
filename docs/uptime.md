# Uptime monitoring and incident alerts

A PyPulse worker calls each scheduled HTTP endpoint from the machine where the worker runs. Keep `python manage.py pulseworker` running alongside the dashboard. For a private API, run the worker on a host that can reach your service. Your framework's PyPulse middleware records real application traffic independently of these synthetic checks.

## Connect a production endpoint

1. Open **Uptime → Add monitor** and give it a recognizable name. Use a read-only health endpoint such as `https://api.example.com/ready`.
2. Set the HTTP method, project, environment and comma-separated tags. Choose an optional network profile to apply that profile's simulated connection conditions. A URL such as `{{BASE_URL}}/ready` resolves using workspace variables for the monitor's environment.
3. Set the interval, timeout, expected status, optional required response text and slow-response threshold. New monitors inherit the workspace's timing and failure defaults. Set the slow threshold to zero to disable slow-response alerts.
4. Open **Request configuration** for name/value headers, query parameters, Bearer, Basic or API-key authentication, a body and JSON assertions. The monitor and benchmark editors share these controls. For a self-signed endpoint, change **TLS verification** for this request. Private monitor values stay write-only. Editing one section replaces that section and preserves the others. **Advanced JSON** accepts the full options object. A blank field preserves existing options and `{}` clears them.
5. Pick the consecutive failure threshold, alert events, destinations, reminder interval and escalation delay. Enable **Show on public status page** if the service name and availability should be public.
6. Choose **Send test request** to make one real request from the dashboard server, using the current form and a five-second timeout ceiling. The preview shows the HTTP status, duration and assertion result. It leaves check history, incident state and notifications untouched. Save and open the monitor. **Check now** makes it due for the next worker pass. Pause and resume scheduling from the detail page. A clone starts paused and private, with fresh history and its original request and routing settings.

Here is a request for an authenticated JSON endpoint. Create the secret workspace variable `API_TOKEN` in the selected environment.

```json
{
  "headers": {"Accept": "application/json"},
  "query": {"region": "eu"},
  "auth": {"type": "bearer", "token": "{{API_TOKEN}}"},
  "statuses": [200],
  "json_equals": {"service.ready": true, "dependencies.0.state": "healthy"}
}
```

The supported methods are GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS. Headers and query values are strings. A body can be plain text or a JSON array/object. For a URL-encoded form, use `"form":{"field":"value"}` in place of `body`. Auth supports `none`, `bearer`, `{"type":"basic","username":"{{USER}}","password":"{{PASSWORD}}"}`, and `{"type":"api_key","name":"X-API-Key","value":"{{KEY}}","in":"header"}`. Use `query` in place of `header` for a query key. Keep URL query credentials in the private options. The dashboard encrypts options and channel credentials with a key derived from the server's Django secret key. Preserve that key with your backups.

The **Status expression** accepts comma-separated codes, ranges and classes such as `200-204, 301, 4xx`. It takes precedence over the private `statuses` list and single expected status. Use `json_checks` for comparisons, for example `[{"path":"orders[0].state","op":"contains","value":"ready"},{"path":"metadata.id","op":"exists"}]`. Supported operators are `equals`, `not_equals`, `contains` and `exists`. The first three compare text; existence requires a present, non-null value. Use `json_equals` for typed equality. Required body text and every configured JSON check must match. Dot paths support nested object keys and zero-based array indexes. The empty path checks the whole JSON value. Headers, query parameters, body fields, auth and URLs can also use the built-in [dynamic request values](benchmarks.md#dynamic-request-values). Assertions inspect up to the first 64 KiB of the response. A network error, timeout or assertion failure counts as a failed check.

## Read availability and latency

**Uptime** shows each monitor's current state, latest check timestamp, latest latency and the fraction of checks that passed in the last 24 hours. Search by name or endpoint URL, then filter by state, project, environment and tag. Group the filtered view by project or environment. Down and slow monitors appear first in each group, with open incidents above the table. A 24-hour field with no completed checks shows **No checks**.

Open a monitor to compare **1h**, **24h**, **7d**, **30d** and **90d**. The numerator is successful scheduled or manual checks. The denominator is completed checks, including failures. Paused periods and intervals with no observations contribute no samples. The chart plots mean and p95 response time per bucket. The uptime strip shows the successful share in each bucket. Gaps indicate unsampled buckets. The summary includes mean latency, nearest-rank p95 and p99, slow checks and incidents opened during the selected range. After raw retention expires, archived UTC hours contribute weighted counts and means. Percentiles are exact for raw observations. When archived histograms are included, the dashboard reports estimated upper bounds from millisecond buckets and the number of observations covered. Legacy archived hours without latency distributions still contribute to the mean and availability. The slow count uses the threshold at check time for samples collected after upgrading to this release. The oldest archived hour crossing the start of a selected range is omitted because its individual timestamps have expired.

The recent-check table shows the HTTP status, duration and a short assertion or transport result. Filter it to failed checks, slow checks, or both, and use the pagination links within the selected time range. While the tab is visible, the dashboard updates the measurement panel in place. The public status page shows published, active monitor names and their state, 24-hour success rates, 90 days of daily check history and current public incidents. Workspace settings control the title and publication switch.

## Configure notifications

Open **Incidents → Alert channels → Add channel**. Choose a destination and enter its private configuration. Each example below uses placeholder values.

| Channel | Private configuration |
| --- | --- |
| Signed webhook | `{"url":"https://alerts.example.com/pypulse","secret":"replace-with-signing-key"}` |
| Slack incoming webhook | `{"url":"https://hooks.slack.com/services/replace-with-your-hook"}` |
| Microsoft Teams webhook or compatible Workflow | `{"url":"https://your-workflow.example.com/replace-with-your-callback"}` |
| SMTP email | `{"host":"smtp.example.com","port":587,"security":"starttls","from":"alerts@example.com","to":["oncall@example.com"],"username":"alerts","password":"replace-with-password"}` |

SMTP supports `starttls`, implicit `tls`, and `plain` for an unauthenticated relay. HTTP notification destinations require HTTPS, with HTTP available on loopback for local receivers. Slack receives a text message and supports an optional `mention`. Webhooks support a `headers` object and an optional signing secret. Teams receives an Adaptive Card. A signed webhook receives JSON containing `monitor`, `event`, `state`, `at`, and `delivery_id`. Verify the hexadecimal HMAC-SHA256 in `X-PyPulse-Signature` against the raw request body. Use `delivery_id` to deduplicate retries.

Save the channel and click **Send test** to queue an actual delivery to that destination. The delivery history refreshes in the open tab. The worker retries failed deliveries up to three attempts. To rotate a credential, enter its replacement in the channel editor. Unedited fields retain their saved values. Credentials never appear on the channel detail screen.

Select standard channels per monitor. Leaving that selection blank uses all enabled standard channels and the optional environment webhook configured with `PYPULSE_ALERT_WEBHOOK` and `PYPULSE_ALERT_SECRET`. For escalation, select one or more explicit escalation destinations or mark fallback channels **Escalation only**. Use the monitor's **Alert events** to restrict which notifications it sends.

## Handle an incident

When consecutive failed checks reach the monitor's threshold, the worker changes its state to **Down**, opens one incident and queues initial alerts. A passing check above the latency threshold opens a **Slow** incident. Moving between Down and Slow closes the earlier incident and opens one for the new condition. Returning to Up closes the active incident and queues a recovery event according to the selected alert rules.

Open **Incidents** to filter open, acknowledged and closed incidents. Open a condition for its timeline and per-incident delivery history. **Resolve** closes the incident with the operator’s name; a manual resolution leaves the measured monitor state intact. Use the page links for older history in this list or on an individual monitor. **Acknowledge** records the operator and timestamp and stops further reminders and escalations. Set a positive reminder interval for repeat alerts while an incident remains open and unacknowledged. A positive escalation delay sends one escalation for the incident. Zero disables either timer.

Open **Uptime → Maintenance** for active, upcoming and past windows across the workspace. Give the window a name, enter a UTC start and end, and choose one or more monitors. Select **All current and future monitors** for workspace-wide maintenance. Use **Edit** to update the times, recurrence or affected monitors. Removal from the overview applies to the full window scope. Use **Schedule maintenance** on an individual monitor for a single-service window. Choose once, daily or weekly. The worker continues measuring availability and tracking incidents during maintenance. It suppresses outgoing alerts during the window. An outstanding reminder or escalation becomes eligible once the window ends. Pause the monitor when you want to stop checks entirely.

## Operational HTTP API

Use the dashboard session cookie. Write requests also require the Django CSRF token in the `X-CSRFToken` header. Submit JSON objects with `Content-Type: application/json`. The endpoints use the same dashboard permissions and UTC times. The CI token API has its own documented scope. These paths omit trailing slashes.

| Method and path | Behavior |
| --- | --- |
| `GET /api/monitors`, `GET /api/monitors/{id}` | List monitors or read the configuration summary, including state and routing IDs. Private request options stay write-only. |
| `POST /api/monitors` | Create a monitor. Required fields are `name` and `url`. |
| `PUT` or `PATCH /api/monitors/{id}` | Update supplied fields. Both verbs retain omitted values. |
| `DELETE /api/monitors/{id}` | Delete this monitor, its checks and incidents. |
| `POST /api/monitors/test` | Send a bounded preview from the supplied monitor fields. Set `id` to reuse a saved monitor’s request. Returns `http`, `ms` and `result`. |
| `POST /api/monitors/{id}/clone` | Create a paused private copy. |
| `POST /api/monitors/{id}/check`, `/pause`, `/resume` | Queue a check, pause scheduling or resume scheduling. A paused monitor returns 409 for a check. |
| `GET /api/monitors/{id}/stats?range=24h` | Returns count, availability, mean, p95, p99, slow/incident counts and timestamped bucket series. `coverage` reports percentile sample coverage and `approximate` marks estimates. |
| `GET /api/monitors/{id}/checks?range=7d&result=issues` | Paginated checks with status, duration, result and a redacted failure category. Supported results are `all`, `issues`, `failed` and `slow`. |
| `GET /api/monitors/{id}/alerts`, `GET /api/alerts?status=open` | Paginated per-monitor or workspace incident history. Workspace filters are `all`, `open`, `active`, `acknowledged` and `resolved`. |
| `POST /api/alerts/{id}/ack`, `/resolve` | Acknowledge an incident or resolve it manually. |
| `GET /api/alerts/{id}/notifications` | Paginated delivery history for the incident. |
| `GET` or `POST /api/channels`; `GET`, `PUT`, `PATCH` or `DELETE /api/channels/{id}` | Read, create, update or remove a destination. Send `name`, `kind` and private `configuration` on creation. |
| `POST /api/channels/{id}/test` | Queue a real test delivery to the enabled channel. |
| `GET` or `POST /api/maintenance`; `GET`, `PUT`, `PATCH` or `DELETE /api/maintenance/{id}` | Manage named UTC windows. Supply `start`, `end`, `recurrence` and either `targets` (monitor IDs) or `all_monitors: true`. |
| `GET /api/public/status` | Public monitor names, availability, daily history and current public incidents. Disabling the status page returns 404 here too. |

Monitor writes accept `method`, `group`, `environment`, comma-separated `tags`, `interval`, `timeout_ms`, `expected_status`, `status_expression`, `contains`, `warn_ms`, `failure_threshold`, `paused`, `public`, `network_profile` (ID), private `options`, `alert_events`, `channels` and `escalation_channels` (ID arrays), `reminder_minutes` and `escalation_minutes`. To patch private options independently, set `options_patch: true` and supply only the changed top-level keys. A null value clears that key. Channels accept `enabled`, `escalation_only` and the private keys shown above, with `config_patch: true` for a partial private update. Paginated JSON responses expose `items`, `page`, `pages` and `total`; `limit` accepts 1–100. Range options are 1h, 24h, 7d, 30d and 90d.

A request payload for a health monitor can be as small as the following.

```json
{
  "name": "Store readiness",
  "url": "{{BASE_URL}}/ready",
  "environment": "production",
  "status_expression": "2xx",
  "options": {
    "auth": {"type": "bearer", "token": "{{API_TOKEN}}"},
    "json_checks": [{"path": "ready", "op": "equals", "value": "true"}]
  }
}
```
