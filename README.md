# DanuBaaS

DanuBaaS provides a sensor backend with a Go API, selectable Node-RED ingestion,
Mosquitto, Telegraf, TimescaleDB, and authenticated Grafana/Node-RED dashboards.

The API exposes one resource for immutable sensor observations.
`POST /api/v1/values` accepts one observation or an atomic batch;
`GET /api/v1/values` returns one observation by ID or an ordered page of observations.
Both methods require an `X-API-Key` header.

**Start the complete development environment with `make dev`.** See the
[short quick start](QUICKSTART.md) or [production configuration](deploy/PRODUCTION.md).

The implementation uses Go's HTTP server and pgx, with PostgreSQL transactions and
TimescaleDB storage. It has no ORM, application framework, or in-memory production
storage fallback. A successful write response means the database committed it.

## Run with the demonstration environment

See [the deployment guide](deploy/README.md) for Compose, Grafana, Node-RED,
MQTT, certificates, and independently authenticated dashboards. Run deployment
commands from the repository root, which is also the Go module and Docker build
context. The module path is `github.com/AndreasWillibaldWeber/DanuBaaS`.

Existing installations should read the [repository migration notes](docs/repository-migration.md)
before recreating containers from the new checkout path.

```sh
git clone https://github.com/AndreasWillibaldWeber/DanuBaaS.git
cd DanuBaaS
make dev
```

For standalone development, use Go 1.25 or newer and a disposable TimescaleDB
instance. The tested compiler for this workspace is Go 1.26.8.

```sh
export DATABASE_URL='postgres://postgres:development-password@localhost:5432/sensors?sslmode=disable'
go run ./cmd/sensor-api migrate
export ALERT_RULES_FILE=deploy/alerts.dev.example.json
export ALERT_EVALUATION_SECONDS=30
go run ./cmd/sensor-api configure-alerts
export API_KEY="$(openssl rand -hex 32)"
export ALERT_ADMIN_KEY="$(openssl rand -hex 32)"
go run ./cmd/sensor-api
```

This standalone example uses an administrative connection to simplify local setup.
The Compose environment separates the migration role from the restricted runtime
role. Never use an administrative database account for a deployed API.

## Resource contract

| Request | Successful response |
| --- | --- |
| `POST /api/v1/values` with an object | `201` and the committed object |
| `POST /api/v1/values` with an array | `201` and committed objects in request order |
| Replay previously committed, identical IDs and content | `200` and the original records |
| `GET /api/v1/values?id=<uuid>` | `200` and one object; `404` if absent |
| `GET /api/v1/values?limit=100&after=0` | `200` and an array, including `[]` when empty |

An array containing both new and replayed records returns `201`. A reused ID with
different content returns `409` and rolls back the **entire** batch. There are no
update or delete operations. API keys authorize both reads and writes across the
demo dataset; per-device authorization is a separate concern from this demonstration.

```json
{
  "id": "12345678-1234-4234-8234-123456789001",
  "sensor_id": "groundwater-01",
  "gateway_id": "demo",
  "sensor_type": "water-level:ground-water",
  "timestamp": "2026-09-14T10:00:00Z",
  "value": 1.42,
  "unit": "m",
  "lon_lat": [16.3738, 48.2082],
  "location_id": 7,
  "metadata": {
    "packet_id": 42,
    "ref_to_zero": 312.5,
    "deviation": 0.02,
    "rssi": -78
  }
}
```

`id` is a lowercase, client-generated UUID. Retain it on retry. `timestamp` is event
time, including a timezone; it is normalized to UTC. Precision beyond microseconds
is rejected because PostgreSQL cannot preserve it. `value` must be a finite number:
zero is valid; missing and null values are rejected. `unit` is required and limited
to 32 UTF-8 bytes. Arbitrary source-specific JSON belongs in `metadata`; unknown
top-level fields are rejected.

Location fields are independent and optional on every observation:

| Field | JSON type and meaning |
| --- | --- |
| `lon_lat` | Two numbers in **[longitude, latitude]** order, WGS84 decimal degrees; longitude −180…180, latitude −90…90 |
| `location_id` | Integer from 0 to 9007199254740991, identifying a location in your own registry |

Either field can be omitted or `null`; both can be supplied together. `[0, 0]`
and ID `0` are valid. A coordinate pair must contain exactly two finite numbers;
partial pairs, strings, and null array elements are rejected. Send `location_id`
as an integer JSON literal. Its bound avoids precision loss in Node-RED.
Missing and null fields are omitted in POST/GET responses and stored as SQL NULL,
so they are interchangeable on retries. Supplying, changing, or removing a location
on an existing observation ID is a conflict. No location registry, coordinate
lookup, or consistency check between the two fields is implemented.
See [location examples and storage details](docs/locations.md).

Responses add `sequence` and server-generated `received_at`. POST bodies must omit
those response-only fields. Do not post an unmodified GET response as a new input.

For example, save the observation as `value.json` and call:

```sh
curl --fail-with-body \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  --data-binary @value.json http://localhost:8080/api/v1/values

curl --fail-with-body -H "X-API-Key: $API_KEY" \
  'http://localhost:8080/api/v1/values?id=12345678-1234-4234-8234-123456789001'
```

Use HTTPS through Caddy for non-local clients. Never send API keys in URLs.

### Reading all values

The list defaults to 100 records and allows `limit=1..1000`. Results are ordered by
commit sequence, not event time, so late-arriving measurements remain discoverable.
Follow the `Link: <...>; rel="next"` header or pass `X-Next-Cursor` as `after` until
there is no next-page header. An empty database returns `[]`. There is deliberately
no unbounded response that can exhaust memory as the dataset grows.

Pagination traverses a live dataset, not a snapshot. The demo serializes database
writers before allocating sequence numbers so a delayed commit cannot be skipped
by a cursor. This is a deliberate correctness/throughput tradeoff for a small demo.

### Validation and errors

- Request body: at most 1 MiB; normalized observation: at most 16 KiB; batch:
  1–1000 unique IDs; JSON depth: at most 32.
- Reject malformed JSON, duplicate keys (including metadata), trailing documents,
  invalid timestamps, unexpected types, invalid identifiers, and unknown queries.
- `400`: malformed request; `401`: missing/incorrect key; `404`: missing record;
  `405`: unsupported method; `409`: ID conflict; `413`: oversized body;
  `415`: unsupported content type/encoding; `422`: invalid observation/batch;
  `503`: database unavailable or operation timed out.
- Errors use `{"error":{"code":"...","message":"..."}}`. Database details and
  credentials are not returned to clients.

The machine-readable contract is [docs/openapi.yaml](docs/openapi.yaml).

Verify the running stack with `make test-e2e`: curl writes through the Go API and
Node-RED, then checks stored observations through both single and paginated reads.
See [end-to-end testing](docs/end-to-end-testing.md) for prerequisites and both routes.

## Persistence and migrations

`internal/postgres/migrations/001_initial.sql` creates:

- `sensor.events`: globally unique IDs, canonical payload JSONB, reception time,
  and the ordered ingestion sequence.
- `sensor.measurements`: a Timescale hypertable partitioned by event time, containing
  queryable measurement fields and metadata.
- `sensor.dashboard_values`: the read-only reporting interface for Grafana.

Both records are written in the same transaction. The registry enforces global ID
uniqueness; a Timescale hypertable alone cannot enforce an ID-only unique index.
JSONB retains JSON meaning, not original whitespace, key order, or numeric spelling.
If original byte-level checksum verification is required, carry the original
payload bytes in metadata (for example as base64) before normalization.

`002_mqtt_ingestion.sql` adds the alternative Telegraf write adapter:

| Object | Purpose |
| --- | --- |
| `sensor.mqtt_ingest(time timestamptz, value text)` | INSERT/COPY interface for a complete JSON object or batch; its BEFORE INSERT trigger consumes the row |
| `sensor.ingest_mqtt()` | Validates and normalizes observations, then writes both existing tables in one transaction |
| `sensor.rejected_messages` | Rejected document, SQL error code/reason, reception time, and identity sequence |

The adapter table stays empty. Its trigger runs with the migration owner's rights
and a fixed search path; the Telegraf role gets only schema usage and INSERT on
that table. It acquires the same writer lock as the Go API before allocating event
sequences. An identical observation can be replayed through either route without
creating another measurement. Invalid data or conflicting IDs roll back the whole
MQTT document and create a quarantine record. Other SQL failures remain errors
so Telegraf can retry them. Quarantine is an operational table, not part of the
public API or dashboard reporting view; inspect it through an administrative
database connection.

This adapter does not change the direct API's response contract. Node-RED's
optional MQTT mode returns `202` after broker acknowledgment and provides eventual
read visibility. See [route selection and delivery semantics](deploy/README.md#select-the-node-red-write-route).

`003_optional_locations.sql` adds nullable `longitude double precision`,
`latitude double precision`, and `location_id bigint` columns to measurements and
appends them to the reporting view. Database constraints enforce coordinate pairs,
ranges, and the location ID bound. The MQTT adapter applies the same location
rules. Existing payloads and any location data inside metadata remain unchanged;
there is no automatic backfill or promotion from metadata.

Migrations are embedded and executed explicitly with `sensor-api migrate` under an
administrative role. They are transactionally versioned and safe to run again.
The HTTP process does not migrate schemas or receive DDL privileges. Future schema
changes must be new migrations; do not edit an applied migration.

No automatic retention policy is installed. The report requires evaluation over
two years, and deleting raw data prematurely would undermine that evaluation.
Registry retention, hypertable retention, and backup retention need one coordinated
policy before deployment at scale.

## Configuration

| Setting | Default / behavior |
| --- | --- |
| `API_KEY` or `API_KEY_FILE` | Required for serving; minimum 32 bytes |
| `ALERT_ADMIN_KEY` or `ALERT_ADMIN_KEY_FILE` | Required for serving; independent key, minimum 32 bytes |
| `ALERT_WEBHOOK_URL` or `ALERT_WEBHOOK_URL_FILE` | Optional HTTPS notification receiver; empty file disables delivery |
| `ALERT_RULES_FILE` | Initial rule array for `configure-alerts`; existing rules are preserved |
| `ALERT_EVALUATION_SECONDS` | Required for `configure-alerts`; interval 1–3600 seconds |
| `DATABASE_URL` | Optional complete PostgreSQL connection string |
| `DB_HOST`, `DB_PORT`, `DB_NAME` | `localhost`, `5432`, `sensors` |
| `DB_USER` | `sensor_api` |
| `DB_PASSWORD` or `DB_PASSWORD_FILE` | Required unless `DATABASE_URL` is provided |
| `DB_SSLMODE` | `require`; Compose uses `disable` on the isolated local network |
| `LISTEN_ADDR` | `:8080` |
| `HEALTH_ADDR` | `:8081`; never publish this listener externally |

For a secret, setting both its value and `_FILE` fails startup. Missing secrets also
fail startup. Secret files are preferable to environment variables in deployment.

The separate operational listener provides `/livez` and `/readyz`. Readiness checks
database connectivity; it is not a schema/permission audit. The container healthcheck
expects the default health port. HTTP header/body deadlines, a 10-second operation
timeout, bounded database connections, and graceful SIGTERM shutdown are configured.

## Development and verification

```sh
make test                 # HTTP, validation, configuration, concurrency; race detector
make vet
make build
# Use only a disposable database, never a production connection:
TEST_DATABASE_URL='postgres://postgres:test@localhost:5432/test?sslmode=disable' make integration
go test ./internal/httpapi -run='^$' -fuzz=FuzzPOSTNeverPanics -fuzztime=10s
```

The integration suite verifies actual migrations, JSON round trips, transaction
rollback, concurrent idempotency, hypertable writes, cursor ordering, and canceled
operations. It **fails**, rather than silently skipping, if the database URL is
missing. The HTTP suite uses a contract fake; passing it is not evidence that a
real database deployment passed.

The MQTT database suite also exercises PostgreSQL COPY, cross-route retries,
timezone normalization, nested null metadata, whole-batch quarantine, concurrent
Go/MQTT writers, and an INSERT-only role. The deployment's separate
`test:telegraf` command verifies the real broker and Telegraf plugins against the
migrated database.

[docs/quality.md](docs/quality.md) records the test strategy and acceptance checks.
GitHub Actions runs unit/race/vet checks plus the disposable TimescaleDB integration
suite from this repository root.

## Scope

This is a demonstrator of authenticated ingestion, retrieval, and configurable
water-level alerts. [Alerting](docs/alerting.md) includes warning/critical level
and rise-rate parameters, PostgreSQL evaluation, history, acknowledgment,
optional webhooks and setup integration. It does not claim
flood forecasting, guaranteed delivery through the Node-RED MQTT adapter, high
availability, per-sensor API-key scopes, or field-validated flood-warning performance.
Those capabilities require separate acceptance criteria and implementation.

The imported backend, deployment, tests, and documentation retain their
[Apache-2.0 license](LICENSES/Apache-2.0.txt). The initial repository material
retains its [MIT license](LICENSE). See [NOTICE](NOTICE) for scope and provenance.
The workshop report and its original images are not included in this repository.
