# Workspace operations

Open **Workspace** after signing in. Administrators can create members, adjust defaults and transfer configuration. Network profiles, reusable variables and personal CI tokens are accessible from the same screen.

## Members and permissions

Open the dashboard on a direct loopback connection for the first-run wizard. Enter your display name, email and a password of at least 12 characters. Your email is the initial account’s sign-in name. For a remote HTTPS install, set a long random `PYPULSE_SETUP_SECRET` on the web process and enter it in the wizard. A reverse proxy counts as a remote connection. The wizard closes as soon as the first account exists. You can also create it from the server console with `python manage.py createsuperuser` or `docker compose exec web python manage.py createsuperuser`.

In **Workspace → Members**, create an account with an initial password, choose its role, or clear **Active** to disable access. The list shows the last sign-in in UTC. **Reset password** opens the Django admin password form for that member. Deactivation removes active browser sessions and disables the account’s CI tokens. Members change their own passwords in **Workspace → My account**. Other sessions are invalidated after a password change. If an administrator loses access, run `python manage.py changepassword USERNAME` on the server to set a replacement interactively.

| Role | Access |
| --- | --- |
| Viewer | Read dashboards, reports, settings, profiles and masked variables. Create read-only CI tokens. |
| Editor | Operate monitors, maintenance windows, incidents, benchmarks, custom network profiles and variables. Export masked configurations, start and stop runs, and create run-scoped CI tokens. |
| Administrator | Manage service connections, tokens, destinations, workspace settings, configuration and members. |

Browser sessions last seven days by default. Set `PYPULSE_SESSION_HOURS` to change their lifetime. Ten failed logins for the same account and direct client address pause sign-in for 15 minutes across the dashboard, Django admin and JSON login endpoint.

The role editor manages the PyPulse Editors group. Existing custom groups and individually assigned Django permissions continue to apply. You can administer granular permissions in Django administration. Keep at least one active administrator.

## Server bootstrap and system details

Run `python manage.py migrate` followed by `python manage.py bootstrap_workspace` for a native installation. The bootstrap command refreshes the built-in Editor group, honors `PYPULSE_ADMIN_EMAIL` and `PYPULSE_ADMIN_PASSWORD` when the installation is empty, and merges a native JSON or YAML document from `PYPULSE_CONFIG_FILE` when supplied. Pass credentials through the deployment secret store. Existing accounts retain their passwords when you restart. In a container, mount the file read-only and use its in-container path.

**Workspace → System** shows host CPU usage, web process memory and uptime, Python and Django versions, active runs and scheduled checks. Administrators can see the data directory to back up. **Network profiles → Duplicate** opens an editable copy of a preset. Add a description to document its intended network conditions. Values and descriptions for **Variables** are scoped to an environment. A secret stays masked when a viewer opens the list or an editor updates its description.

## Defaults, history and status

**Workspace → Workspace settings** controls the initial check interval, request timeout, slow threshold and consecutive failures for new monitors. Edit an existing monitor to change its behavior. Use **Public status** to set the title of `/status/` or disable the entire public page. Enable **Show on public status page** on each monitor you want to publish.

Retention settings apply to raw checks (30 days by default), hourly uptime summaries (365 days), finished benchmarks (90 days) and Python traffic batches (7 days). The worker performs retention periodically. Retention also determines how much history is available to reports.

In **Benchmark targets**, list the hosts you operate that can receive test traffic. Examples include `api.staging.example:8443` and `*.staging.example:443`. A portless hostname permits any port on that host, and a wildcard matches subdomains. These entries supplement `PYPULSE_TEST_HOSTS`. The worker must be able to reach the approved host at the address configured in the test.

## JSON and YAML transfer

1. In **Workspace → Configuration transfer**, select sections and choose JSON or YAML. Both formats use the same `pypulse.workspace/v1` schema.
2. Export settings, network profiles, scoped variables, alert channels, monitors, saved benchmarks or maintenance windows. The file uses names for cross-references, including benchmark profiles, standard alert channels and the escalation destination. Give monitors and benchmarks distinct names before transferring them.
3. The default download masks secret variables, channel destinations and credentials, private request options, query-bearing URLs and CSV fixtures. The `{"$redacted": true}` marker preserves the matching local value when imported. To restore into an empty installation, select **Include plaintext credentials and private request data** on a trusted workstation, or supply the missing values manually. Store a plaintext export in an encrypted secret store.
4. Select the JSON or YAML file, or paste one document in the import form, then choose which of its sections to apply. **Merge** updates matching records and creates missing ones. **Replace** additionally deletes records missing from sections present in both the upload and your selection. An empty list clears that section. Removing a monitor deletes its observations and incidents. The page requires confirmation for Replace. References from a benchmark with history or a retained monitor can prevent a deletion.
5. Review the import result. One invalid row rolls back the entire import. Built-in profiles remain available. Matching variables use their name and environment, and maintenance windows match by start instant, scope and optional name. A window can target one named monitor, a list of monitors, or every monitor. An empty target list retains an inactive window after its last target is removed. Older single-monitor files still import.

Users, API tokens, connected service credentials and measurements remain in the database backup. Configuration files describe the settings and reusable tests. Uploads accept up to 1 MiB. In YAML, quote date strings and use explicit mappings without aliases.

## CI performance gate

1. Create and run a benchmark in the dashboard with the thresholds your release must meet. Note its numeric ID from its edit URL.
2. Under **Workspace → CI tokens**, issue a named token with **Start tests and read results**. Copy the token into your pipeline's secret store. The dashboard stores a hash and can show the token prefix and last use. Revoke any token individually. A read-scoped token can poll a result, and the existing `PYPULSE_CI_TOKEN` server setting remains supported.
3. Put the dashboard origin in `DASHBOARD`, the benchmark ID in `TEST_ID` and the token in `PYPULSE_TOKEN`. This shell example requires `curl` and `jq` and gives the worker up to five minutes after its initial wait.

```sh
set -eu
gate=$(curl -fsS -X POST -H "Authorization: Bearer $PYPULSE_TOKEN" \
  "$DASHBOARD/api/ci/tests/$TEST_ID/run?wait=15")
report=$(printf '%s' "$gate" | jq -er '.report')
for attempt in $(seq 1 150); do
  if printf '%s' "$gate" | jq -e '.finished' >/dev/null; then break; fi
  sleep 2
  gate=$(curl -fsS -H "Authorization: Bearer $PYPULSE_TOKEN" "$DASHBOARD$report")
done
printf '%s\n' "$gate" | jq '{id,state,finished,passed,report}'
printf '%s' "$gate" | jq -e '.finished and .passed' >/dev/null
```

The POST accepts `wait=0` through `20` seconds. If the test finishes during the wait, the response is HTTP 200 with `finished: true` and its pass decision. Otherwise it is HTTP 202 with a report path in `Location`, `Retry-After: 2` and `passed: null`. Poll that report with GET (HTTP 200). A failed threshold still produces a readable report. The shell command exits unsuccessfully for a failed or unfinished gate. A measurements-only run has `finished: true` and `passed: null` because it has no gate. Each POST starts a separate run.

The saved-template route `POST /api/templates/{id}/run?wait=true&timeoutSec=15` also accepts the bearer token. It bounds each wait at 20 seconds and returns the report path if work is still in progress.

## Workspace JSON API

Use a signed-in session for workspace management. Get `/api/auth/status` to receive the CSRF cookie and a `csrfToken`. Send that token in `X-CSRFToken` along with the cookie and a JSON body when posting to `/api/auth/login`. Use the updated cookie and token returned after login or a password change for subsequent writes. Browser clients on the same origin can use the session cookie automatically.

| Endpoint | Operations |
| --- | --- |
| `/api/auth/status`, `/api/auth/me` | Read first-run status and the current account. |
| `/api/auth/setup`, `/api/auth/login`, `/api/auth/password`, `/api/auth/logout` | POST first-run credentials, sign in, change your own password, or sign out. Password changes use `currentPassword` and `newPassword`. |
| `/api/users`, `/api/users/{id}` | Administrators list, create, update and delete users. PUT accepts `name`, `role`, `disabled` and optional replacement `password`. |
| `/api/tokens`, `/api/tokens/{id}` | GET your tokens, POST a name and scope to issue one, DELETE to revoke. The creation response includes its one-time secret. |
| `/api/profiles`, `/api/variables` | GET resources, POST to create, and PUT or DELETE by numeric ID. Built-in profiles stay read-only. Masked secret updates preserve the stored value. |
| `/api/settings`, `/api/system` | Read settings or process diagnostics. Administrators can PUT partial `defaults`, `retention`, `loadAllowlist` and `statusPage` sections. |
| `/api/config/export`, `/api/config/import` | GET a masked YAML or JSON file with `format=yaml` or `format=json`. Administrators can request `secrets=true`. POST a document body to import with `mode=merge` or `mode=replace`. Repeat `section` query parameters to limit the scope. |

To preview a one-off test, POST a benchmark definition to `/api/tests/preflight`. The response gives the approved host, estimated requests, model and duration. For example, a fixed test at one request per second for two seconds uses `{"name":"Readiness","url":"http://localhost:8000/health","rate":1,"seconds":2}`. Editors can POST the same definition with `"confirm":true` to `/api/tests/run` to queue it without creating a saved template.

Manage saved tests at `/api/templates`. Removing a template keeps its reports and their recorded names. An operator session with run-change permission can POST to `/api/runs/{id}/stop`. A queued run stops immediately. A running worker observes the stop request and finalizes the report. GET `/api/runs/{id}/raw` to download gzipped UTF-8 CSV measurements using a session or an active read/run CI bearer token. The export includes sample timings, step/profile labels, statuses and transfer sizes. Request credentials and bodies stay private.

For example, create a CI credential by posting `{"name":"release","scope":"run"}` to `/api/tokens` as an editor or administrator. Set an existing variable’s description by sending `{"description":"Used by the checkout pipeline"}` to `PUT /api/variables/{id}`.

## Public HTTPS deployment

Point your dashboard hostname at your server. Ensure public TCP 80 and 443 reach the server. Set `PYPULSE_DOMAIN` to the hostname in the project's `.env` and use the supplied Compose overlay.

```sh
docker compose -f compose.yaml -f compose.https.yaml up --build -d
docker compose -f compose.yaml -f compose.https.yaml exec web python manage.py createsuperuser
```

Open the dashboard at your HTTPS hostname. Caddy obtains and renews the certificate and proxies to the web container. The overlay configures the accepted hostname, secure cookies and trusted proxy scheme. Keep the web port bound to loopback, as set by the base Compose file. The worker and web share the same data volume and signing key.

For a native deployment with your own certificate, set `PYPULSE_HTTPS=1` and an appropriate `PYPULSE_HOSTS` hostname, then pass the certificate and private key to your ASGI server. This example binds to loopback.

```sh
uvicorn pulse.asgi:application --host 127.0.0.1 --port 8443 \
  --ssl-certfile /path/to/fullchain.pem --ssl-keyfile /path/to/privkey.pem
```

## Backup and restore

For a native deployment, stop the web and worker processes, copy the complete `PYPULSE_DATA_DIR` (default `data/`) to encrypted storage, then restart the processes. Keep the configured `PYPULSE_SECRET_KEY` in your secret store if it overrides the generated `data/signing.key`. The same key decrypts stored request credentials and variables after restore.

For Compose, run the following from the project directory. Stop the two writers before archiving the shared SQLite data and its WAL files. With HTTPS, keep using the same pair of Compose files when managing the deployment.

```sh
umask 077
docker compose stop web worker
docker compose run --rm -T --no-deps --entrypoint tar web -C /app/data -czf - . > pypulse-data.tgz
docker compose start web worker
```

Restore into a fresh deployment with an empty volume and the original secret settings. Feed the archive to a container that mounts that volume, then start both services. On an upgrade the setup container applies migrations. Keep the backup and its encryption key separately protected.

```sh
docker compose run --rm -T --no-deps --entrypoint tar web -C /app/data -xzf - < pypulse-data.tgz
docker compose up -d
docker compose exec web python manage.py check
```

Sign in, verify a monitor and its saved history, and run a test against your staging host. For a full HTTPS deployment backup, also preserve the Caddy data and configuration volumes and your environment settings.

### Import an initial SQLite workspace

`python manage.py import_workspace /path/to/old.sqlite3` imports supported accounts, service tokens, route aggregates and completed fixed-GET results into an empty migrated installation. Imported editor accounts receive the PyPulse Editors role. Repeating the command with the same database snapshot leaves current records and roles in place. This one-time migration supports a narrower schema than the native database backup.
