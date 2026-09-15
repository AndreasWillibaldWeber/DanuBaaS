# Deployment and operations

The supported development entry point is `make dev`; production uses `make prod`
after a read-only preflight. Both commands run from the repository root. The
[full deployment guide](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/deploy/README.md)
records Compose startup, route selection, upgrade steps, TLS endpoints, and
backup/restore details.

## Production preflight

Copy `deploy/.env.production.example` to `deploy/.env.production`, then set the
public hostnames, account names, ingestion backend, evaluation interval, and
absolute paths for secrets and MQTT certificates. Production credentials must
be supplied in external secret files; startup never generates them. Use an
operator-supplied alert-rule file, starting from an empty array if thresholds
have not been calibrated.

```sh
make prod-check
make prod
```

`make prod-check` validates configuration, distinct credentials, file permissions,
certificate/key/hostname matching, and Compose requirements before contacting
Docker. `make prod` performs the same checks, then builds and starts the
production Compose project. Read the
[production configuration reference](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/deploy/PRODUCTION.md)
for every required file and port. The development and production projects have
separate volumes but publish the same ports; stop development before starting
production on the same host.

## Health and alert delivery

The API has a separate private listener: `/livez` and `/readyz` on port 8081.
Readiness tests database connectivity. Keep that listener, PostgreSQL, and plain
MQTT off public networks. The authenticated `GET /api/v1/alerting-status` reports
evaluator freshness and notification backlog; it returns `503` if evaluation has
not succeeded within three schedule intervals. Watch the TimescaleDB job stats
and Grafana evaluator panels as well. An independent monitor is needed to detect
a complete database/API outage.

Webhook delivery is optional and at least once. Outbox events can accumulate
while delivery is disabled; inspect the backlog before enabling a receiver on an
existing installation. Receivers should deduplicate the `Idempotency-Key` header.
See [alert notifications and health](alerting.md#notifications-and-health).

## Backups, retention, and upgrades

For a development stack, run the one-shot backup job:

```sh
docker compose -f deploy/compose.yaml --profile backup run --rm backup
```

It writes a custom-format PostgreSQL dump under `deploy/backups`. Store an
encrypted copy off-host, along with the deployment's secrets, certificates,
configuration, and stateful Node-RED/Grafana/Caddy volumes.
Test a restore into a fresh disposable database before treating a backup as
recoverable. The [deployment guide's restore procedure](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/deploy/README.md#persistence-and-operations)
includes TimescaleDB pre/post-restore calls, `pg_restore --no-owner`, role/grant
reapplication, and data comparisons.

No automatic measurement retention policy is installed; a retention change needs
a coordinated policy for registry rows, hypertables, and backups. Apply new
migrations explicitly before starting upgraded writers. Do not edit applied
migrations or delete volumes to upgrade. Existing Node-RED flow volumes may need
a manual import after a flow change; the
[upgrade instructions](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/deploy/README.md#upgrade-an-existing-deployment)
identify when this is required.
