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
