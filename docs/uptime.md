# Uptime monitoring and incident alerts

A PyPulse worker calls each scheduled HTTP endpoint from the machine where the worker runs. Keep `python manage.py pulseworker` running alongside the dashboard. For a private API, run the worker on a host that can reach your service. Your framework's PyPulse middleware records real application traffic independently of these synthetic checks.

## Connect a production endpoint

1. Open **Uptime → Add monitor** and give it a recognizable name. Use a read-only health endpoint such as `https://api.example.com/ready`.
2. Set the HTTP method, project, environment and comma-separated tags. Choose an optional network profile to apply that profile's simulated connection conditions. A URL such as `{{BASE_URL}}/ready` resolves using workspace variables for the monitor's environment.
3. Set the interval, timeout, expected status, optional required response text and slow-response threshold. New monitors inherit the workspace's timing and failure defaults. Set the slow threshold to zero to disable slow-response alerts.
4. Use **Private request options** for authentication, headers, query parameters, a request body and additional assertions. Paste a JSON object. On edit, a blank field preserves the configuration. Enter `{}` to clear it.
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

The supported methods are GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS. Headers and query values are strings. A body can be text or a JSON object. Auth supports `none`, `bearer`, `{"type":"basic","username":"{{USER}}","password":"{{PASSWORD}}"}`, and `{"type":"api_key","name":"X-API-Key","value":"{{KEY}}","in":"header"}`. Use `query` in place of `header` for a query key. Keep URL query credentials in the private options. The dashboard encrypts options and channel credentials with a key derived from the server's Django secret key. Preserve that key with your backups.

`statuses` overrides the single expected status. Required body text and every configured JSON check must match. Dot paths support nested object keys and zero-based array indexes. The empty path checks the whole JSON value. Assertions inspect up to the first 64 KiB of the response. A network error, timeout or assertion failure counts as a failed check.

## Read availability and latency

**Uptime** shows each monitor's current state, latest check timestamp, latest latency and the fraction of checks that passed in the last 24 hours. Search by name or endpoint URL, then filter by state, project, environment and tag. State and search filters combine with the project, environment and tag. A 24-hour field with no completed checks shows **No checks**.

Open a monitor to compare **1h**, **24h**, **7d**, **30d** and **90d**. The numerator is successful scheduled or manual checks. The denominator is completed checks, including failures. Paused periods and intervals with no observations contribute no samples. The chart shows mean duration per time bucket. Gaps indicate unsampled buckets. The summary includes mean latency, nearest-rank p95 and p99, slow checks and incidents opened during the selected range. After raw retention expires, archived UTC hours contribute weighted counts and means. The percentiles use retained individual observations. The slow count uses the threshold at check time for samples collected after upgrading to this release. The oldest archived hour crossing the start of a selected range is omitted because its individual timestamps have expired.

The recent-check table shows the HTTP status, duration and a short assertion or transport result. Filter it to failed checks, slow checks, or both, and use the pagination links within the selected time range. While the tab is visible, the dashboard updates the measurement panel in place. The public status page shows published, active monitor names and their state and 24-hour success rates. Workspace settings control the title and publication switch.

## Configure notifications

Open **Incidents → Alert channels → Add channel**. Choose a destination and enter its private configuration. Each example below uses placeholder values.

| Channel | Private configuration |
| --- | --- |
| Signed webhook | `{"url":"https://alerts.example.com/pypulse","secret":"replace-with-signing-key"}` |
| Slack incoming webhook | `{"url":"https://hooks.slack.com/services/replace-with-your-hook"}` |
| Microsoft Teams webhook or compatible Workflow | `{"url":"https://your-workflow.example.com/replace-with-your-callback"}` |
| SMTP email | `{"host":"smtp.example.com","port":587,"security":"starttls","from":"alerts@example.com","to":["oncall@example.com"],"username":"alerts","password":"replace-with-password"}` |

SMTP supports `starttls`, implicit `tls`, and `plain` for an unauthenticated relay. HTTP notification destinations require HTTPS, with HTTP available on loopback for local receivers. Slack receives a text message. Teams receives an Adaptive Card. A signed webhook receives JSON containing `monitor`, `event`, `state`, `at`, and `delivery_id`. Verify the hexadecimal HMAC-SHA256 in `X-PyPulse-Signature` against the raw request body. Use `delivery_id` to deduplicate retries.

Save the channel and click **Send test** to queue an actual delivery to that destination. The delivery history refreshes in the open tab. The worker retries failed deliveries up to three attempts. Changing a credential requires submitting the replacement configuration. Credentials never appear on the channel detail screen.

Select standard channels per monitor. Leaving that selection blank uses all enabled standard channels and the optional environment webhook configured with `PYPULSE_ALERT_WEBHOOK` and `PYPULSE_ALERT_SECRET`. For escalation, select an explicit channel or mark destination channels **Escalation only**. Use the monitor's **Alert events** to restrict which notifications it sends.

## Handle an incident

When consecutive failed checks reach the monitor's threshold, the worker changes its state to **Down**, opens one incident and queues initial alerts. Passing checks recover the incident. A passing but slow check produces **Slow**. The first passing check after an outage sends **Recovered**. A slow endpoint that returns below its threshold produces **Up**.

Open **Incidents** to filter open, acknowledged and recovered incidents. Use the page links for older history in this list or on an individual monitor. **Acknowledge** records the operator and timestamp and stops further reminders and escalations. Set a positive reminder interval for repeat alerts while an incident remains open and unacknowledged. A positive escalation delay sends one escalation for the incident. Zero disables either timer.

Open **Uptime → Maintenance** for active, upcoming and past windows across the workspace. Give the window a name, enter a UTC start and end, and choose one or more monitors. Select **All current and future monitors** for workspace-wide maintenance. Removal from the overview applies to the full window scope. Use **Schedule maintenance** on an individual monitor for a single-service window. Choose once, daily or weekly. The worker continues measuring availability and tracking incidents during maintenance. It suppresses outgoing alerts during the window. An outstanding reminder or escalation becomes eligible once the window ends. Pause the monitor when you want to stop checks entirely.
