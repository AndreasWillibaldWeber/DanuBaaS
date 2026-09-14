# Quality strategy

The critical invariant is that a successful API write represents a committed,
immutable observation and that a failed batch cannot leave a partial result.
Authentication must be checked before any storage access.

| Concern | Verification |
| --- | --- |
| One object and batch behavior | HTTP create/read/replay tests |
| Missing/wrong/duplicate API key | Both supported and unsupported HTTP methods |
| Atomic validation | Invalid last item must not invoke the store |
| Atomic persistence | Real database conflict after a new item rolls everything back |
| Retry safety | Same ID/content returns original sequence and reception time |
| Race conditions | Race detector and concurrent retries against fake and real stores |
| HTTP end-to-end | Real curl single/batch writes through Go and Node-RED, eventual GET visibility, cross-endpoint content equality, retry identity, and paginated lists; CI Compose matrix for both backends |
| Optional locations | HTTP and Node-RED: missing/null independently, both fields, zero, bounds, wrong types, pair length/order, and invalid final item |
| Location persistence | Go and MQTT: typed columns, GET/list, reporting view, mixed batches without field leakage, cross-route retries, location conflicts, and database constraints |
| Location transport | Real Node-RED and Telegraf preserve coordinates and IDs; explicit nulls become absent canonical fields and SQL NULL |
| Data quality | Missing/null/zero, timezone normalization, precision, overflow |
| Hostile inputs | Duplicate JSON keys, excessive depth/size, trailing JSON, wrong types |
| Cursor traversal | Stable ordering, next headers, empty arrays, invalid cursors |
| Secret handling | No credentials in HTTP errors; exclusive value/file configuration |
| Availability | Database errors return 503; cancellation aborts work |
| Schema | Migration rerun; real hypertable row and JSON metadata checks |
| Dashboard isolation | Separate credentials/session, expiry, CSRF origin, logout and sockets |
| Selectable ingestion | Real Node-RED runtime in API and MQTT modes; no write fan-out |
| MQTT REST authentication | Missing/wrong key rejected locally, including route case/slash variants |
| Broker acknowledgment | Complete batch in one QoS 1 message; no acceptance before PUBACK; bounded pending packets and timeout recovery |
| Cross-route retries | Concurrent Go and MQTT submissions create one measurement and preserve original identity |
| MQTT batch isolation | Invalid final item or conflicting ID rolls back the document and produces quarantine |
| Telegraf permissions | Actual COPY through a role with only schema usage and staging INSERT |
| Telegraf plugin behavior | Real broker and Telegraf: JSON metadata, replay, conflict quarantine, and progress after poison messages |
| Development bootstrap | Fresh setup, preserved existing credentials, unavailable dependencies, verified write/read, and failure exit status |
| Production preflight | Every required setting/secret, placeholder and duplicate rejection, hash format, filesystem permissions, isolated project, and no generation/start on validation failure |
| Certificate validation | Real OpenSSL certificates: matching hostname/key and rejection of hostname mismatch, mismatched key, and imminent expiry |

The test database must be disposable. Integration tests create persistent test data
with unique IDs; they do not truncate arbitrary databases. CI supplies a fresh
TimescaleDB service. Never point `TEST_DATABASE_URL` at production.

## Release acceptance

1. Run unit tests, race detector, static analysis, a short fuzz run, and integration tests.
2. Build containers and validate the Compose model and Caddy configuration.
3. From outside the host, verify that only HTTPS, MQTT TLS, and intended administration
   ports are reachable. Database, API health, and plain MQTT ports must stay private.
4. Verify Caddy credentials alone cannot read either dashboard. Verify dashboard
   credentials alone cannot bypass Caddy. Exercise a real Socket.IO connection.
5. POST through the Go and Node-RED endpoints, then read the committed values from
   both dashboards. Check single/batch requests, retries, and key rejection.
6. Restart API/database, replay the same observation, and confirm no duplicate.
7. Publish a real MQTT payload and verify it appears after database commit.
   Repeat with `INGESTION_BACKEND=api` and `mqtt`, using fresh IDs and then replaying
   identical IDs across routes. In MQTT mode expect HTTP `202`, then poll GET until
   visible. Stop Telegraf, submit a batch, restart it, and check eventual storage.
   Stop the broker and verify Node-RED returns `503`. Submit a conflicting batch,
   confirm no partial write, and inspect `sensor.rejected_messages`. Verify broker
   ACLs prevent the demo publisher from writing the normalized topic and prevent
   Telegraf from publishing. Test queue exhaustion and restart recovery separately
   before claiming durable delivery.
8. Create a backup and restore it into a fresh disposable database. Reapply roles
   and grants, then compare observation counts, representative metadata, and optional
   locations. Verify pre-upgrade observations still replay with omitted or null locations.

See the deployment guide for environment-specific verification limitations. A
passing mock-based test is not a substitute for these deployment checks.

Run `make test-deploy` for bootstrap/preflight contracts. OpenSSL tests use real
temporary certificates. With Node-RED dependencies installed, the suite also runs
the real development credential generator twice in a temporary directory and
compares every secret and certificate. The startup smoke-test contract uses a
controlled HTTP peer; only a successful `make dev` against Docker verifies the
complete startup. In this workspace, `make dev` currently stops at the missing
Docker Compose plugin prerequisite, so full bootstrap startup remains unverified.

## Optional location verification

The location change was checked with the Go race detector and static analysis,
HTTP validation/read/retry tests, database integration tests, both actual Node-RED
runtime routes, and real Mosquitto/Telegraf ingestion. A separate disposable
version-2 database verified that migration `003` preserves existing payloads,
metadata, IDs, timestamps, and sequences; the new columns remain NULL and old
observations replay with omitted or null locations.

Local database checks used PostgreSQL 16 without the TimescaleDB extension. They
verify SQL behavior and transport, but do not establish hypertable migration or
full container-stack compatibility. CI's TimescaleDB suite and the deployment
acceptance checks above remain required before release.

Run `make test-e2e` against the running development stack for HTTP acceptance.
The [end-to-end guide](end-to-end-testing.md) describes assertions, retained test
data, CI coverage, and the distinction between local PostgreSQL verification and
full Compose/TimescaleDB execution.

## Alerting verification

Migration `004` and the alert lifecycle suite were run against a disposable native
TimescaleDB **2.27.1 on PostgreSQL 16.15**. This included real hypertables,
Go and MQTT database writers, warning/critical transitions, hold/hysteresis,
missing sensors, late readings, configuration version conflicts, idempotent seeds,
acknowledgments, notification leasing and the restricted scheduler role.

A separate native startup exercise ran the migration, deployment grants and
`configure-alerts` twice, started the Go API as `sensor_api`, and verified actual
scheduled detection over HTTP, both health routes, administrative key isolation,
version conflicts and acknowledgment. This validates the real scheduler and
application integration; it does not replace the pinned PostgreSQL 17 Compose
build/startup checks. Docker daemon access was unavailable in this workspace.
CI's existing Compose matrix runs `make dev` for both ingestion backends and now
also requires a successful scheduled evaluation through HTTPS.

Webhook transport tests use a real local TLS receiver and check successful and
failed deliveries, stable idempotency identifiers, and redirect rejection.
Bootstrap tests cover alert parameter validation, new credentials, preserved rule
files, and fresh/repeated setup. Full deployment verification still requires
`make dev` with an accessible Docker daemon.

## Debian host-security tooling

`make test-security` validates policy rendering, input rejection, firewall trial
ordering, rollback, persistence confirmation and failure recovery without changing
the host. The real nftables network test uses isolated namespaces and checks IPv4
and IPv6 SSH source restrictions, allowed web/MQTT DNAT, forbidden internal ports,
direct-container access, container egress, repeat loading and scoped restoration.
These tests passed in the development workspace; shell syntax, generated APT
configuration and systemd unit checks are also part of the local review.

This is not evidence of a completed Debian server rollout. SSH effective policy,
fail2ban integration, update behavior, timed systemd recovery, actual Docker
networking and reboot persistence still require the disposable-VM acceptance
steps in [the host-security guide](../security/debian/README.md).
