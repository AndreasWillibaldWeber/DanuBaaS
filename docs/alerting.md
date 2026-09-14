# Water-level alerts

DanuBaaS evaluates committed measurements inside PostgreSQL using PL/pgSQL and a
TimescaleDB scheduled job. Both Go API and MQTT/Telegraf ingestion feed the same
rules. The API manages configuration, reads history, accepts acknowledgments, and
optionally delivers webhooks. Grafana shows active alerts and evaluator health.

## Configuration and setup

`make dev` creates `deploy/alerts.json` from `deploy/alerts.dev.example.json`,
provisions independent `alert_admin_key` and `alert_webhook_url` secret files,
applies database migrations/grants, and runs the `configure-alerts` service.
This service inserts missing initial rules and configures the existing scheduler
job. Existing rules, versions, disabled settings, credentials and volumes are
preserved on rerun. Change existing rules through the API: editing the seed file
does not override database configuration. Keep the initial file valid even after
rules have been imported, because preflight validates it on each startup.

The development rule monitors the synthetic `quickstart` sensor:

| Parameter | Development example | Meaning |
| --- | --- | --- |
| `level_warning` | 2.0 | Warning level, metres |
| `level_critical` | 3.0 | Critical level, metres |
| `rise_warning` | 0.1 | Warning rise, metres per configured period |
| `rise_critical` | 0.2 | Critical rise, metres per configured period |
| `rise_period_seconds` | 300 | Rate units: metres per five minutes |
| `rise_window_seconds` | 600 | Maximum history used to find a baseline |
| `rise_min_seconds` | 30 | Minimum separation between rate samples |
| `level_hysteresis` | 0.05 | Recovery margin in metres |
| `rise_hysteresis` | 0.02 | Recovery margin in rate units |
| `hold_seconds` | 0 | Required observation-time duration before opening/escalating |
| `stale_seconds` | 900 | Maximum age of a usable reading; also initial reporting grace |

These are demonstration parameters, not calibrated flood-warning thresholds.
Production starts with the empty `alerts.production.example.json` or an
operator-supplied rule array. Set `ALERT_RULES_FILE` to an absolute readable file
outside the repository and `ALERT_EVALUATION_SECONDS` to 1–3600 in
`.env.production`. Supply the two alert secret files before `make prod-check`.
No production sensor or threshold is invented by setup.

`ALERT_EVALUATION_SECONDS` defaults to 30 for development. Rule thresholds live in
the database, not environment variables. Configuration changes take effect on the
next evaluation. Each rule is uniquely associated with one sensor; registering a
rule also registers the expectation that the sensor should report.

## Measurement and state semantics

Only observations with **`sensor_type="water-level"` and `unit="m"`** are usable.
Convert distance-to-water readings and other units upstream using the appropriate
site reference before ingestion. Other readings remain stored, but cannot open,
clear or keep an alert fresh.

For each new usable reading, the rate is:

```text
rate = (current level - baseline level)
       / (current observation time - baseline observation time in seconds)
       * rise_period_seconds
```

The baseline is the earliest committed reading of the same sensor/type/unit
within the window, at least `rise_min_seconds` before the current reading. This
is an average rise over the available interval, not an instantaneous derivative.
Evidence records both observation IDs and the calculated rate. Equal timestamps
are never divided; insufficient history leaves rise status unchanged and resets
its pending hold. A previously firing rise condition stays open until there is
valid evidence of recovery or a rule change; loss of data is reported separately.

A value **at or above** warning triggers warning, and at or above critical triggers
critical. An active condition stays at its current severity until below that
severity's threshold minus hysteresis. Escalation/downgrade updates the same alert
occurrence. Recovery resolves it. Later breaches create new occurrences. Hold
applies to opening/escalation, uses distinct valid observations, and does not
advance merely because the scheduler polls the same sample. A stale gap resets
pending holds. Recovery and downgrade are immediate once hysteresis is satisfied.

Missing data produces a separate warning, including for sensors that have never
reported. It does not resolve existing level/rise alerts. Fresh usable data clears
the stale condition. Acknowledgment records receipt; it does not resolve a hazard.
The first acknowledgment is preserved on retry.

Evaluation processes at most 1000 new arrivals per rule per run, sorted by
observation time within each batch. Progress is stored transactionally. Older or
equal timestamps do not rewind current state. Future-dated, already stale, and
pre-configuration observations are skipped permanently as live triggers, while
remaining available as historical data. Historical backfills are not replayed as
live alarms. Monitor throughput and evaluation delay before increasing workload.

Editing a rule increments its version, records the authenticated principal, and
retires active conditions with a `configuration_changed` event. Evaluation then
waits for new observations under the new configuration; the stale grace restarts.
Old rule snapshots and triggering evidence remain attached to historical alerts.

## API

Use the direct custom API host (`https://api.localhost` in development). The
Node-RED `flows.localhost` host continues to expose only sensor ingestion/reads.
Every request requires one `X-API-Key` header. `api_key` permits alert reads;
`alert_admin_key` permits reads and administrative writes. Neither key is printed
by setup. Existing installations upgrading through `make dev` can find the new
administrator key in `deploy/secrets/alert_admin_key`.

| Method and route | Operation |
| --- | --- |
| `GET /api/v1/alert-rules` | List rules; string `after` cursor and `limit` |
| `POST /api/v1/alert-rules` | Create with all parameters and `version: 0` |
| `GET /api/v1/alert-rules/{id}` | Read current parameters and version |
| `PUT /api/v1/alert-rules/{id}` | Replace parameters using the current version |
| `GET /api/v1/alert-rules/{id}/versions` | Read configuration history |
| `GET /api/v1/alerts?active=true` | List active occurrences |
| `GET /api/v1/alerts/{id}` | Read an occurrence |
| `GET /api/v1/alerts/{id}/events` | Read transitions and evidence |
| `POST /api/v1/alerts/{id}/acknowledgments` | Acknowledge with `{"note":"Investigating"}` |
| `GET /api/v1/alerting-status` | Evaluator freshness, rule progress and notification backlog |

For replacement, use the full rule body, retain `id`, `sensor_id`, and current
`version`, and omit server-managed `updated_at`. Concurrent edits receive `409`;
reload and reconcile before retrying. Disable with `enabled:false`; history is not
deleted. Numeric lists accept `after` and `limit` (1–1000, default 100); use the
last returned ID, or version for version history, until an empty page. Pagination
reads live data. Administrative bodies are limited to 16 KiB.

The shared administrative credential is recorded as **`alert-admin`**, not as an
individual human identity. Individual operator attribution requires a future
identity-provider/session integration. The API never trusts a caller-supplied
actor name. See [openapi.yaml](openapi.yaml) for the machine-readable contract.

## Notifications and health

An empty `alert_webhook_url` secret disables external delivery. To enable it,
put an HTTPS receiver URL in that file and recreate/restart the API. URL secrets
are never included in logs. The receiver gets JSON containing the immutable event,
its alert occurrence and rule snapshot. The occurrence reflects its current state
at delivery time; use `event.details.severity` for the event-time severity.
Redirects are not followed. A 2xx response acknowledges delivery.

Event creation and outbox insertion commit together. The sender leases rows for
60 seconds and retries failures with bounded exponential delay. Delivery is
**at least once**: receivers must deduplicate `Idempotency-Key`. Pending events
accumulate even when delivery is disabled, and will be sent when enabled. Review
that backlog before enabling a receiver on an established installation. The
sender runs with the API process; evaluation continues during API downtime.

The existing private API listener serves `/livez` and `/readyz` on port 8081.
Readiness checks database connectivity. The authenticated `/api/v1/alerting-status`
returns `503` when no evaluation has succeeded within three schedule intervals;
notification failures do not make sensor ingestion unready. It includes the
pending count and oldest pending event time. Rule progress is limited to the first
100 rule IDs in this diagnostic response. Grafana has active-alert and evaluator
health panels; configuration and acknowledgment are currently API operations.

Inspect `timescaledb_information.jobs`, `job_stats`, and `job_errors` for scheduler
failures. The job has a 20-second maximum runtime and a restricted login-capable
owner with no assigned password, as required by TimescaleDB background workers.
Network authentication in deployment requires credentials. Procedures perform no
external network calls. A shared transaction lock serializes evaluation and edits;
a unique index permits only one active occurrence per rule/condition.

An external monitor must check API/database availability and notification delivery:
the database cannot deliver an alert about its own complete outage. This feature
provides threshold monitoring, not validated flood prediction or field evaluation.

## Verification

Run `make test`, `make vet`, `make test-deploy`, and `make integration` with a
disposable TimescaleDB database supplied as `TEST_DATABASE_URL`. Tests cover both
writers, severity transitions, hysteresis, hold, stale/missing sensors, historical
arrivals, version conflicts, seed preservation, acknowledgments, outbox leasing,
and the restricted evaluator role. `make dev` additionally waits for a successful
scheduled evaluation via the HTTPS status route. CI runs `make dev` in both
API and MQTT deployment modes.
