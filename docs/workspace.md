# Workspace operations

Open **Workspace** after signing in. Administrators can create members, adjust defaults and transfer configuration. Network profiles, reusable variables and personal CI tokens are accessible from the same screen.

## Members and permissions

Start with `python manage.py createsuperuser`, or run `docker compose exec web python manage.py createsuperuser`. In **Workspace → Members**, create an account with an initial password, choose its role, or clear **Active** to disable access. The list shows the last sign-in in UTC. **Reset password** opens the Django admin password form for that member. Deactivation also disables the account's CI tokens.

| Role | Access |
| --- | --- |
| Viewer | Read dashboards and reports, create read-only CI tokens. |
| Editor | Operate monitors, maintenance windows, incidents and benchmarks. Start and stop runs, create run-scoped CI tokens. |
| Administrator | Manage service connections, tokens, destinations, workspace settings, configuration and members. |

The role editor manages the PyPulse Editors group. Existing custom groups and individually assigned Django permissions continue to apply. You can administer granular permissions in Django administration. Keep at least one active administrator.

## Defaults, history and status

**Workspace → Workspace settings** controls the initial check interval, request timeout, slow threshold and consecutive failures for new monitors. Edit an existing monitor to change its behavior. Use **Public status** to set the title of `/status/` or disable the entire public page. Enable **Show on public status page** on each monitor you want to publish.

Retention settings apply to raw checks (30 days by default), hourly uptime summaries (365 days), finished benchmarks (90 days) and Python traffic batches (7 days). The worker performs retention periodically. Retention also determines how much history is available to reports.

In **Benchmark targets**, list the hosts you operate that can receive test traffic. Examples include `api.staging.example:8443` and `*.staging.example:443`. A portless hostname permits any port on that host, and a wildcard matches subdomains. These entries supplement `PYPULSE_TEST_HOSTS`. The worker must be able to reach the approved host at the address configured in the test.

## JSON and YAML transfer

1. In **Workspace → Configuration transfer**, select sections and choose JSON or YAML. Both formats use the same `pypulse.workspace/v1` schema.
2. Export settings, network profiles, scoped variables, alert channels, monitors, saved benchmarks or maintenance windows. The file uses names for cross-references, including benchmark profiles, standard alert channels and the escalation destination. Give monitors and benchmarks distinct names before transferring them.
3. The default download masks secret variables, channel destinations and credentials, private request options, query-bearing URLs and CSV fixtures. The `{"$redacted": true}` marker preserves the matching local value when imported. To restore into an empty installation, select **Include plaintext credentials and private request data** on a trusted workstation, or supply the missing values manually. Store a plaintext export in an encrypted secret store.
4. Select the JSON or YAML file, then choose which of its sections to apply. **Merge** updates matching records and creates missing ones. **Replace** additionally deletes records missing from sections present in both the upload and your selection. An empty list clears that section. Removing a monitor deletes its observations and incidents. The page requires confirmation for Replace. References from a benchmark with history or a retained monitor can prevent a deletion.
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

The POST accepts `wait=0` through `20` seconds. If the test finishes during the wait, the response is HTTP 200 with `finished: true` and its pass decision. Otherwise it is HTTP 202 with a report path in `Location`, `Retry-After: 2` and `passed: null`. Poll that report with GET (HTTP 200). A failed threshold still produces a readable report. The shell command exits unsuccessfully for a failed or unfinished gate. Each POST starts a separate run.

## Public HTTPS deployment

Point your dashboard hostname at your server. Ensure public TCP 80 and 443 reach the server. Set `PYPULSE_DOMAIN` to the hostname in the project's `.env` and use the supplied Compose overlay.

```sh
docker compose -f compose.yaml -f compose.https.yaml up --build -d
docker compose -f compose.yaml -f compose.https.yaml exec web python manage.py createsuperuser
```

Open the dashboard at your HTTPS hostname. Caddy obtains and renews the certificate and proxies to the web container. The overlay configures the accepted hostname, secure cookies and trusted proxy scheme. Keep the web port bound to loopback, as set by the base Compose file. The worker and web share the same data volume and signing key.

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
