# Measure an endpoint or a user journey

Benchmarks send real HTTP requests from the PyPulse worker. They are separate from the passive Python middleware: you can measure an endpoint without installing the SDK there. To see the effect in **Traffic**, connect the target app's middleware too.

1. Start the dashboard and its worker (`docker compose up -d` or, locally, `python manage.py pulseworker` beside the web process).
2. Approve a target reachable **from the worker** in workspace settings or `PYPULSE_TEST_HOSTS`; include its port, e.g. `api.staging.example:443`. Within Compose, localhost is the worker container. Use the target's service hostname on the shared network.
3. Open **Benchmarks → New benchmark**. Start with a default GET endpoint, **Fixed**, **Arrival rate**, **5 iterations/s**, **15 seconds**, and a p95 goal of **300 ms**. Save, then choose **Run**. This schedules 75 single-step arrivals. Use **Stop run** to interrupt the worker.
4. Open the report for requests/s, p50/p95/p99, active iterations, and error rate. Completed reports add a latency histogram, DNS/TCP/TLS/TTFB/download timings, per-step and per-profile breakdowns, a paginated timeline and full sample CSV. Choose **Compare two runs** to select two finished load runs or two latency runs. The comparison shows percentage changes, throughput and p95 overlays and metrics for matching network profiles. A failed release gate lists each missed threshold.

## Pick a workload

| Workload | Use it to measure |
| --- | --- |
| Fixed | A steady arrival rate, or a fixed number of virtual users repeatedly performing the scenario. |
| Ramp | Start small and increase toward the target. Set **starting load**, **increment** and **seconds per increment** together for a staircase. |
| Stress | Increase load and stop on an error-rate/p95 violation or (optionally) a throughput plateau. The report identifies the attempted load and reason. |
| Spike | Compare a lower baseline with a brief peak in the middle. |
| Soak | Hold a steady workload to expose progressive slowdown. |
| Latency | Run sequential iterations; specify **count**, **delay**, **timeout** and one or more network profiles. |

Arrival rates count **scenario iterations**, not steps. With two steps, a successful iteration records two requests. Virtual users wait for their own previous iteration to finish; set **delay** for think time. A failed step stops that iteration and contributes one failure. Unstarted steps are not counted as successful responses. A dropped arrival means the worker could not start it on schedule, usually because all configured concurrency slots were busy.

Custom stages override the built-in load curve. For a 30-second test:

```json
[{"seconds":10,"target":2},{"seconds":15,"target":5},{"seconds":5,"target":2}]
```

For arrival tests, target means iterations per second. For virtual users it means concurrent iterations. For an explicit ramp, set the start, increment and step duration, then choose how many seconds to hold the final target. For a spike, set the baseline load and the durations of the baseline, peak and recovery. Under these explicit timing controls, the stage durations set the total run time.

A stress test can use the release gates for stop conditions or use independent error-rate and p95 stop thresholds. Independent thresholds need at least 20 requests in the current stage for error rate or 20 completed HTTP responses for p95. The worker checks at stage boundaries and during longer stages after three seconds. Enable **Finish stress after one full stage at the maximum load** to advance through every configured increment. **Plateau windows** specifies how many successive higher stages must fall short of the selected growth percentage, relative to the best passing stage, before the run stops.

The stage table records the target and measured requests/s, p95 and failure count. A request is counted in the stage where its result completes. A breaking point records the statistics at the stop decision. **Highest passing stage** reports the greatest observed throughput among stages that passed. **Average throughput** divides all attempts by elapsed run time. **Steady throughput** is the median rate of full reporting windows after excluding the first and last when at least three windows exist.

New latency tests run selected profiles concurrently by default with one sequential request stream per profile. Choose **Run one profile at a time** to measure them separately. Load trials run profiles sequentially. Each profile starts with the same fixture sequence. Latency mode uses one iteration per configured second when count is blank. The live report shows the latest sample and completed iterations for each profile. The finished report overlays request latency by profile and includes phase timing bars, histograms, errors, transferred bytes, compressed response counts and connection reuse. Large charts display the slowest answered request per bucket. The table and CSV retain individual samples.

The generator records host CPU, CPU used by the worker's scheduling thread, and delay in waking its event loop. Two reporting windows above 85% host CPU, above 90% scheduling-thread CPU, or above 200 ms of scheduling delay produce a warning. Set `PYPULSE_CPU_WARN_PCT` on the worker to change the host CPU warning threshold.

## Chain requests with a CSV and response data

Set the default target to an approved staging endpoint. If using workspace variables, create **BASE_URL** in the chosen environment (e.g. `https://api.staging.example`). Set the test's **Environment** to that scope. Choose **Add step** to edit the method, URL, expected statuses, headers and extractions visually. Use **Move up/down** to change the request order. Alternatively, open **Edit scenario as JSON** and paste:

```json
[
  {
    "name": "Find order",
    "method": "GET",
    "url": "{{BASE_URL}}/orders/{{order_id}}",
    "auth": {"type":"bearer","token":"{{SERVICE_TOKEN}}"},
    "expected_status": 200,
    "extract": {"item_id":"json:item.id","request_id":"header:X-Request-ID"}
  },
  {
    "name": "Read item",
    "url": "{{BASE_URL}}/items/{{item_id}}",
    "headers": {"X-Request-ID":"{{request_id}}"}
  }
]
```

Create **SERVICE_TOKEN** as a secret workspace variable. Supply the following fixture as UTF-8 CSV by upload or paste:

```csv
order_id
42
73
```

Choose sequential or random rows. Random mode draws with replacement and resets the run-specific random seed for each profile, so compared profiles see the same draws. Each iteration has an isolated copy of workspace variables plus its fixture row. Use a JSON path (including `json:$.orders[0].id`), a header, or a regular expression such as `regex:order=(\d+)`. A regex returns its first capturing group, or the full match when it has no capturing group. The matcher limits expression length to 512 characters and matching time to 25 ms. A missing value stops the iteration. JSON objects and arrays are compactly serialized and extracted values are limited to 4,096 characters. Later steps receive the value only in that iteration. Extraction reads at most the first 64 KiB of a response. Templates can also be used in `query`, `headers`, `body` (text or JSON), `form` (URL-encoded fields), or auth. Supported auth types are `bearer`, `basic` (`username`, `password`) and `api_key` (`name`, `value`, `in: header|query`). Set **Acceptable HTTP statuses** at test level or specify `expected_status` / `expected_statuses` (e.g. `"200-299,304"`) on a step. By default 200–399 succeeds if the transport completed. Redirects are never followed.

Step names and profile names appear in reports and CSV; use descriptive non-secret labels. Keep credentials in workspace variables and use inert fixture data. PyPulse seals the queued plan with the dashboard key, resolves variables when the worker starts, and rechecks each expanded URL against the allowlist before sending. Reports contain timings, statuses and byte counts, not expanded URLs, credentials, response bodies or extracted values.

## Set a release gate or schedule

A completed run passes only if at least one request was sampled, no arrivals were dropped, and the configured error, p95 and minimum average-throughput criteria pass. Zero **maximum errors** means any failed sample fails the gate. Zero disables an individual latency limit. You can set maximum mean, p95 and p99. Latency tests evaluate every selected profile independently. Transport failures count against the error rate, while latency percentiles and phase averages use completed HTTP responses, including HTTP error statuses. **Measurements only** records the results with a neutral gate for exploratory runs. Interrupted, stopped, capped, and threshold-stopped runs do not pass.

- **Repeat every N minutes** queues the saved test automatically when the single worker is online. Alternatively use **Daily run at UTC time** for a fixed daily schedule. Leave both blank/zero for manual runs only.
- **CI:** issue a token with run scope. `POST /api/ci/tests/<test-id>/run?wait=10` using `Authorization: Bearer <token>`. Poll the returned `report` path (`GET /api/ci/runs/<run-id>`) until `finished` is true; fail your pipeline when `passed` is false. An unfinished run returns HTTP 202, so a successful HTTP response alone is not a passing benchmark.

The single worker caps actual requests at 50 per second, at most 20 concurrent iterations, 30,000 total requests and four hours of wall time. The per-profile load duration limit is one hour. Multi-step or high-concurrency tests may reach these limits before your server does. Treat the observed ceiling as the capacity measured **from this worker**, not the absolute capacity of the target. Network profiles simulate delay, connection loss and bandwidth at the client; they do not reproduce every behavior of a mobile carrier. Reported transport phase times omit simulated delays, so they need not sum to elapsed time.

## Dynamic request values

Use built-in placeholders in the URL, headers, query parameters, auth, form or body. A placeholder has one value throughout a request and gets a fresh value for the next request. A UUID in both an `X-Request-ID` header and JSON body will match. The three clock values refer to the same instant.

| Placeholder | Value |
| --- | --- |
| `{{$timestamp}}` | Unix time in whole seconds |
| `{{$timestampMs}}` | Unix time in milliseconds |
| `{{$isoTimestamp}}` | UTC ISO 8601 timestamp with millisecond precision |
| `{{$uuid}}` | Random version 4 UUID |
| `{{$randomInt}}` | Random whole number from 0 through 999,999 |

The editor checks the reserved `$` names when saving. Ordinary placeholders such as `{{order_id}}` still resolve from workspace variables, CSV data or earlier response extractions.
