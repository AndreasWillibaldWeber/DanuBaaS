# Sensor backend demonstration

For the shortest setup, use [QUICKSTART.md](../QUICKSTART.md): `make dev` installs,
configures, starts, and checks the development environment. Production uses
`make prod` with [explicit configuration and preflight checks](PRODUCTION.md).

This Compose project runs Grafana, FlowFuse Dashboard on Node-RED, a separate Go
API, Mosquitto, Telegraf, and TimescaleDB. Caddy protects both browser dashboards with Basic
Auth **in addition to** their application login. Machine endpoints use API keys.

The original workshop report remains unchanged. Its diagrams informed the service
boundaries. Node-RED can write through the Go API or through Mosquitto and
Telegraf. Both routes store the same immutable observations in TimescaleDB;
Grafana and Node-RED Dashboard show committed data from either route.

```mermaid
flowchart LR
  REST[REST client] -->|API key| NR[Node-RED]
  MQTT[MQTT client] -->|raw/v1/...| M[Mosquitto]
  M -->|raw topics| NR
  NR -->|INGESTION_BACKEND=api| Go[Go API]
  NR -->|INGESTION_BACKEND=mqtt: normalized/v1/batch| M
  M -->|normalized topic| T[Telegraf]
  Go --> DB[(TimescaleDB)]
  T -->|transactional SQL adapter| DB
  DB --> G[Grafana]
  DB -->|Go read API| D[Node-RED Dashboard]
```

## Layout

```text
DanuBaaS/                       Go module and Docker build context
  deploy/
    compose.yaml                Services, isolated networks, volumes, secrets
    caddy/                      HTTPS and outer Basic Auth
    nodered/                    Pinned dashboard dependencies, flows, session login
    grafana/                    Read-only data source and provisioned dashboard
    postgres/                   Separate runtime roles and explicit grants
    mosquitto/                  TLS listener and per-publisher topic ACL
    telegraf/                   MQTT consumer and PostgreSQL output
    scripts/                    Local setup, backup, proxy integration tests
```

## Start locally

The recommended command is `make dev` from `DanuBaaS/`. It requires Python 3.9+
and Make in addition to the tools below, preserves existing credentials and data,
exports Caddy's local CA, and verifies a fresh sample through the Node-RED REST
endpoint. `INGESTION_BACKEND=mqtt` also verifies eventual read visibility after
broker acceptance. Failure exits nonzero and leaves diagnostic containers and
volumes intact. A failed smoke check does not mean a submitted value was lost.

After startup, run `make test-e2e` to verify single/batch writes and reads through
both HTTPS endpoints using curl, including optional locations and pagination.
See the [end-to-end test guide](../docs/end-to-end-testing.md) for both backend modes,
configuration, and failure diagnostics. Each run leaves 12 synthetic observations.

The following individual steps are provided for troubleshooting and manual setup.

Requirements: Docker Engine with access to its daemon, **Docker Compose v2.20+**,
Node.js 22.9+ with npm, and OpenSSL. The old Python `docker-compose` v1 command is
not supported. Commands below run from the **`DanuBaaS/` directory** and initialize a fresh
deployment. For an initialized workspace, use [the upgrade steps](#upgrade-an-existing-deployment).

```sh
npm ci --prefix deploy/nodered
node deploy/scripts/setup.mjs
cp deploy/.env.example deploy/.env
cp deploy/alerts.dev.example.json deploy/alerts.json
mkdir -p deploy/backups
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/compose.yaml up --build -d
```

Setup refuses to overwrite existing credentials or certificates. If this workspace
has already been initialized, skip `setup.mjs`. Independent random credentials
are in `deploy/secrets/operator-credentials.json`; read that file locally to sign
in. Credentials are never printed by setup. Secret/certificate directories and
npm dependencies are excluded from version control.

### Select the Node-RED write route

Set **one** value in `deploy/.env`:

```dotenv
INGESTION_BACKEND=mqtt
```

| Value | Write path | Successful Node-RED POST |
| --- | --- | --- |
| `api` (default) | Node-RED → Go API → TimescaleDB | `201` committed; `200` identical replay |
| `mqtt` | Node-RED → Mosquitto → Telegraf → TimescaleDB | `202` accepted by the broker |

The selection applies to Node-RED REST writes and observations received on raw
MQTT topics. It takes effect when the container is recreated:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --force-recreate node-red
```

The direct Go API remains available in both modes. Node-RED GET requests and its
dashboard still use the Go read API; Grafana reads the common reporting view.
Telegraf runs in both modes, ready for a switch. Only Node-RED can publish to its
normalized topic. There is no simultaneous fan-out to both write paths.
On first startup, check `docker compose -f deploy/compose.yaml logs telegraf`
and confirm its MQTT connection before sending data. A persistent subscription
must exist before Mosquitto can queue messages for an offline Telegraf consumer.

### Upgrade an existing deployment

When upgrading an installation that predates Telegraf, add its credentials without
changing existing ones. Build the new images and apply all pending migrations
and grants before starting the updated ingestion services:

```sh
node deploy/scripts/add-telegraf-secrets.mjs
node deploy/scripts/add-alert-secrets.mjs
# Only create the initial rule file if it does not already exist.
test -f deploy/alerts.json || cp deploy/alerts.dev.example.json deploy/alerts.json
docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d timescaledb
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm migrate
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm --no-deps grants
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm --no-deps configure-alerts
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm --no-deps mosquitto-init
docker compose --env-file deploy/.env -f deploy/compose.yaml restart mosquitto
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

Existing Node-RED volumes retain the previous flows. Export a backup from the
editor, then import `deploy/nodered/flows.json`, replacing the existing demo flow
and matching configuration nodes. Preserve any custom flows. Review the import
and deploy it; there must be only one GET and one POST HTTP In node for
`/api/v1/values`. The new demo flow contains **Select ingestion backend**.
Rebuilding the image alone does not update persisted flows. Do not delete volumes
to upgrade: migrations `002` and `003` retain all existing observations.

For an installation already using selectable ingestion, the location-field upgrade
requires rebuilding images and running the migration, then recreating services using
`up -d` as above. No flow reimport or new credentials are needed for this upgrade.
Migration `003` adds nullable location columns and updates the MQTT adapter; existing
observations remain readable and replayable. See [location compatibility](../docs/locations.md).

Local Compose secrets are bind-mounted files, not an encrypted secret store. The
host secrets directory is private (`0700`); individual mounted secret files are
readable by the different container UIDs. Keep backups of these files encrypted
and restrict host access. Containers only receive the secrets they need.

| URL / interface | Authentication |
| --- | --- |
| `https://grafana.localhost` | Caddy `viewer`, then Grafana `admin` |
| `https://nodered.localhost/dashboard` | Caddy `viewer`, then dashboard `operator` |
| `https://api.localhost/api/v1/values` | `X-API-Key` |
| `https://flows.localhost/api/v1/values` | `X-API-Key`; Node-RED HTTP In demonstration |
| `http://127.0.0.1:1880/admin` | Native Node-RED editor `admin` login |
| MQTT `mqtt.localhost:8883` | TLS CA verification, `demo-gateway` user/password and ACL |

Grafana's username/password login remains enabled; its HTTP Basic and auth-proxy
modes are disabled. Caddy consumes its Basic credentials and removes the
`Authorization` header before forwarding dashboard requests. Grafana's session
cookie remains intact.

FlowFuse Dashboard does not supply a standalone local user database by itself.
`nodered/auth.js` implements the second login using bcrypt password verification
and random server-side sessions through Dashboard's supported HTTP and Socket.IO
middleware hooks. The editor has its separate native `adminAuth` configuration.
A Caddy password cannot authenticate either application.

### Trust the local HTTPS CA

Caddy creates an internal CA for `.localhost` names. Once running, export its
**public certificate** (never its private key):

```sh
docker compose -f deploy/compose.yaml cp \
  caddy:/data/caddy/pki/authorities/local/root.crt deploy/certs/caddy-root.crt
```

Trust this development CA in your browser/OS using your normal certificate
management procedure. For curl, pass `--cacert deploy/certs/caddy-root.crt`.
Use `--resolve api.localhost:443:127.0.0.1` if your environment does not resolve
`.localhost` subdomains. Do not disable certificate verification.

For production, configure `deploy/.env.production` and the required external secret
and certificate directories as described in [PRODUCTION.md](PRODUCTION.md).
Use `make prod` to validate and start it. Caddy can obtain public HTTPS certificates. Keep
80/443 reachable for the configured ACME challenge method. Use SSH forwarding to
access the Node-RED editor remotely, for example
`ssh -L 1880:127.0.0.1:1880 operator@server`.

## Demonstrate the API

The same URL handles both single and batch writes and single/list reads. See the
[Go API contract](../README.md) and [OpenAPI document](../docs/openapi.yaml).

Save this as `value.json`:

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
  "metadata": {"packet_id": 42, "deviation": 0.02, "rssi": -78}
}
```

In a local shell:

```sh
API_KEY=$(cat deploy/secrets/api_key)
curl --fail-with-body --cacert deploy/certs/caddy-root.crt \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  --data-binary @value.json https://api.localhost/api/v1/values

curl --fail-with-body --cacert deploy/certs/caddy-root.crt \
  -H "X-API-Key: $API_KEY" \
  'https://api.localhost/api/v1/values?id=12345678-1234-4234-8234-123456789001'

curl --fail-with-body --cacert deploy/certs/caddy-root.crt \
  -H "X-API-Key: $API_KEY" 'https://api.localhost/api/v1/values?limit=100'
```

For a batch, send an array of these objects with distinct UUIDs. A retry must retain
the original ID and content. To demonstrate Node-RED's REST endpoint, replace
`api.localhost` with `flows.localhost` in the same requests. No Caddy Basic Auth is
required for either machine endpoint.

In MQTT mode, Node-RED returns, for example:

```json
{"status":"accepted","backend":"mqtt","ids":["12345678-1234-4234-8234-123456789001"]}
```

This response confirms Mosquitto's PUBACK, **not a database commit**. Poll the
same GET-by-ID request until the observation appears (a temporary `404` is normal).
An unavailable broker, publish timeout, or full publisher queue returns `503`;
retry with the original IDs and content. A timeout does not prove non-delivery.
Identical replays are harmless across both paths. An asynchronous ID conflict
rejects the whole batch into `sensor.rejected_messages`; it cannot return a later
`409` to an HTTP request that has already received `202`.

To confirm storage and inspect rejected documents:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail=100 node-red telegraf
docker compose --env-file deploy/.env -f deploy/compose.yaml exec timescaledb \
  psql -U postgres -d sensors -c 'SELECT id, observed_at, value FROM sensor.measurements ORDER BY observed_at DESC LIMIT 10;'
docker compose --env-file deploy/.env -f deploy/compose.yaml exec timescaledb \
  psql -U postgres -d sensors -c 'SELECT sequence, received_at, error_code, reason FROM sensor.rejected_messages ORDER BY sequence DESC LIMIT 10;'
```

The direct Go endpoint is authoritative for strict wire-format validation.
Node-RED HTTP In nodes parse and reserialize JSON, so details such as duplicate
JSON keys or original whitespace may already be normalized before the Go service
receives them. Use the direct API when testing raw-body validation or preserving
byte-level evidence. Body limits apply at both interfaces.

The list endpoint is bounded: follow `Link` / `X-Next-Cursor` to read all pages.
The Node-RED dashboard consumes up to 1000 committed records per poll and displays
the latest 100 received so far. Grafana charts water-level history and lists recent
observations; select the time range matching your real data.

## MQTT demonstration

Setup generates a dedicated local MQTT CA and a server certificate valid for
90 days. Its SANs include `mqtt.localhost`, `localhost`, and `127.0.0.1`. Set
`MQTT_HOST` before the initial setup if you need a different hostname. The CA key
stays outside the directory mounted into Mosquitto.

```sh
MQTT_PASSWORD=$(cat deploy/secrets/mqtt_demo_password)
mosquitto_pub -h localhost -p 8883 --cafile deploy/certs/ca.crt \
  -u demo-gateway -P "$MQTT_PASSWORD" -q 1 \
  -t raw/v1/demo/groundwater-01 -f value.json
```

The payload may be an object or array in the Go API schema. Every observation's
`gateway_id` and `sensor_id` must match its topic. The demo publisher can only write
`raw/v1/demo/+`; Node-RED subscribes to raw topics and may publish only to
`normalized/v1/batch`. Telegraf may only subscribe to that normalized topic. Add separate broker
users and topic ACLs for additional gateways.

Node-RED forwards observations through the selected backend. Telegraf uses a
persistent MQTT session and QoS 1 with delivery tracking through its PostgreSQL
output. It carries a whole JSON object or array as a single string metric, so the
SQL adapter retains batch boundaries and nested metadata. Its database role has
only schema usage and INSERT on `sensor.mqtt_ingest`; it cannot create tables,
read observations, or write measurements directly.

The adapter commits an entire document into the existing event registry and
hypertable or records the rejected document and its reason. Transient SQL failures
propagate to Telegraf for retry. Broker persistence and QoS 1 reduce loss, but do not
provide end-to-end exactly-once delivery: queues are bounded, Node-RED's raw MQTT
adapter has no durable retry queue, and the broker saves periodically. Retain
observations at the sender and confirm them through GET when delivery matters.

The report's wire fields map as follows: `sid` → `sensor_id`, `gid` → `gateway_id`,
`sensor-type` → `sensor_type`, `time-stamp` → `timestamp`, and `water-level` →
`value` with unit `m`. Map the report's `lon-lat` pair to top-level `lon_lat`
in [longitude, latitude] order and its numeric location ID to `location_id`.
Both fields are optional and accept `null`, independently. Preserve packet `id`,
checksum/hash, `ref-to-zero`, deviation, and RSSI in `metadata`. The API observation UUID is a
separate retry identity; a resetting packet counter is not globally unique.
The demo expects this canonical schema; legacy payload adapters can be added as
versioned Node-RED flows once actual examples are available.

## Persistence and operations

- TimescaleDB, Mosquitto data, Node-RED flows, Grafana state, and Caddy state use
  named volumes. `docker compose down` preserves them; `down -v` destroys them.
- Node-RED seeds its versioned flows and credential **references** into `/data`
  only on first start. Subsequent editor changes persist. Export reviewed changes
  back into the repository; rebuilding an image does not overwrite live flows.
- Dashboard sessions are in memory, limited to 128, and expire after eight hours.
  Restarting Node-RED logs dashboard users out. Logout revokes attached sockets.
  Login/logout require the configured origin; login attempts are rate-limited.
- Database initialization creates roles once. Migrations and grants run as explicit
  one-shot services before the API starts. The API has SELECT/INSERT and sequence
  privileges; Grafana can read only the reporting view. Neither has schema ownership.
  The upgrade-safe grants service creates Telegraf's restricted role if absent.
  Telegraf cannot read the quarantine table; operators should monitor it and define
  a retention policy for rejected payloads.
- Changing a database password file does not change an existing database role.
  Coordinate role/password rotation. Grafana's initial admin password similarly
  applies only when its database is first initialized.
- Restart API and Node-RED after rotating the shared API key. Restart Caddy after
  rotating its password hash. Mosquitto credentials require rerunning its init
  service and restarting the broker.
- Renew the MQTT certificate before expiry and reload/restart Mosquitto. The setup
  script creates a demo certificate; it is not an automatic renewal service.
- Only Caddy 80/443 and MQTT TLS 8883 are published publicly. The editor binds to
  loopback; database, plaintext MQTT, and health endpoints have no host mapping.
  Verify these boundaries against the host's Docker/firewall configuration.
- Local container traffic uses HTTP/PostgreSQL without TLS on dedicated networks.
  Add authenticated TLS between services if they move across hosts or trust zones.
- Runtime images are pinned by digest. An upgrade requires deliberate digest
  updates, dependency review, migration/restore testing, and integration tests.

### Backups

```sh
docker compose -f deploy/compose.yaml --profile backup run --rm backup
```

The one-shot job writes a custom-format PostgreSQL dump to `deploy/backups`, then
renames the temporary file only after success. Schedule it with the host's systemd
timer or backup scheduler. Copy backups off-host and encrypt them. Also preserve
Node-RED/Grafana/Caddy volumes, configuration, and secrets separately.

For restoration, use a **fresh disposable database** with the same PostgreSQL and
TimescaleDB versions. Create the TimescaleDB extension, call
`SELECT timescaledb_pre_restore();`, restore with `pg_restore --no-owner`, and call
`SELECT timescaledb_post_restore();`. Recreate required roles and apply `grants.sql`.
Then compare counts and representative JSON payloads before promoting a restored
instance. Do not restore over an initialized production schema.

## Verification

```sh
make test vet
make test-deploy
npm test --prefix deploy/nodered
NODE_RED_BIN=/path/to/node-red/red.js npm run test:runtime --prefix deploy/nodered
CADDY_BIN=/path/to/caddy python3 deploy/scripts/test_caddy.py
docker compose -f deploy/compose.yaml config --quiet
```

The real Node-RED integration test launches Node-RED with the installed FlowFuse
package in both modes and exercises HTTP plus actual Socket.IO handshakes. Its
controlled API and MQTT peers verify routing, key rejection, and broker
acknowledgments. Caddy integration tests run
the actual Caddyfile with local test upstreams and verify Basic Auth, header
stripping, preserved cookies, machine routes, and editor isolation.

To run real database integration tests locally, create a disposable TimescaleDB
container with a loopback-only port, then run:

```sh
TEST_DATABASE_URL='postgres://postgres:test@127.0.0.1:55432/test?sslmode=disable' \
  make integration
```

The API repository's CI creates this database automatically. The release checklist
is in [quality.md](../docs/quality.md).

After migrations, test real Mosquitto and Telegraf against that disposable database:

```sh
TEST_DATABASE_URL='postgres://postgres:test@127.0.0.1:55432/test?sslmode=disable' \
MOSQUITTO_BIN=/path/to/mosquitto TELEGRAF_BIN=/path/to/telegraf PSQL_BIN=/path/to/psql \
  npm run test:telegraf --prefix deploy/nodered
```

This test starts temporary loopback listeners, creates a temporary restricted
database role, and exercises batches, metadata, retries, conflicts, and poison
messages. It leaves test observations in the disposable database. Its temporary
broker allows anonymous connections; deployed Mosquitto uses the configured TLS,
passwords, and ACLs, which require a separate container smoke test.

**Earlier ingestion verification status (before alerting):** Go unit/race tests, fuzzing, static analysis,
Node-RED session tests, real Node-RED/FlowFuse integration, Caddy integration,
and modern Compose configuration validation have been run. Database contract and
MQTT adapter tests passed on temporary PostgreSQL 16, including restricted-role
COPY and concurrent retries across both writers. Real Telegraf 1.36.4 and
Mosquitto 2.0.18 passed the broker-to-database test. This does not verify the pinned
Mosquitto 2.0.22 container or the TimescaleDB extension. Full container builds, real TimescaleDB integration, live
Grafana, and backup restoration have not been verified in this session because
Docker daemon access is unavailable (direct access denied; sudo requires a
password). These checks must pass before treating this as a verified deployment.

## Scope and sources

The environment demonstrates ingestion, retrieval, independently protected
browser dashboards, and [database-driven alerting](../docs/alerting.md). Warning
and critical level/rise rules, configuration versions, acknowledgment/history,
and optional webhook delivery are implemented. Intervention tracking and field
evaluation remain outside the implementation.
Do not present it as a finished flood-warning installation.

Implementation references:

- [Node-RED runtime security](https://nodered.org/docs/user-guide/runtime/securing-node-red)
- [FlowFuse Dashboard HTTP and Socket.IO middleware](https://dashboard.flowfuse.com/user/settings.html)
- [Caddy Basic Auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth)
- [Caddy upstream headers](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#headers)
- [Timescale unique indexes](https://docs.timescale.com/use-timescale/latest/hypertables/hypertables-and-unique-indexes/)

The imported code/configuration retains its [Apache-2.0 license](../LICENSES/Apache-2.0.txt).
See [NOTICE](../NOTICE) for the repository's license scope. The original report
and images are separate material.

## Alert setup and upgrades

`make dev` provisions new alert secrets without rotating old credentials, creates
`alerts.json` if missing, applies migration `004`, and runs `configure-alerts`
before the API becomes ready. The initial rule file inserts missing rules only;
existing rule edits and disabled flags are preserved. `make prod` uses the explicit
production rule file and scheduler interval. Manual Compose users must supply
`alerts.json`, `alert_admin_key` and `alert_webhook_url` before starting. See
[alerting](../docs/alerting.md) for the API, rate units and notification semantics.
