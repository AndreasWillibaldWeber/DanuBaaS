# Verify a running deployment with curl

From the repository root, start the development stack and run:

```sh
make dev
make test-e2e
```

The verifier uses **curl for every HTTP request** and Python 3.9+ to check responses.
It requires curl, the running services, `deploy/secrets/api_key`, and the local CA
exported by `make dev`. It uses the Go API at `https://api.localhost` and Node-RED at
`https://flows.localhost`. It never prints the key, puts it in process arguments,
follows redirects, or disables TLS verification.

A successful run exits zero and ends with:

```text
PASS: 12 persisted observations verified through both endpoints and paginated lists; run=<unique-run-id>
```

Each run writes 12 synthetic observations, with fresh UUIDs and a unique
`metadata.test_run` marker. Use a development or disposable test deployment.
Observations remain stored because the API is append-only; the verifier does not
delete records, reset volumes, change credentials, or switch ingestion modes.

## What it verifies

- Both endpoints reject unauthenticated GET and POST requests.
- Single-object and batch writes through each endpoint become readable through
  **both** endpoints, with their complete expected content.
- Coordinates and location IDs survive storage; missing and null fields are
  omitted, zero values remain valid, and mixed batches remain independent.
- MQTT `202` acceptance is followed by polling until the observation becomes
  visible. A POST response alone cannot pass the storage check.
- Cross-route retries are accepted; reads retain original sequences and reception
  timestamps. Omitted and explicit null locations are interchangeable.
- Invalid final observations prevent partial batch writes. A location conflict
  through the synchronous Go API returns `409` and leaves the fresh batch item absent.
- Paginated list reads return all test records unchanged, without duplicate IDs or
  a stalled cursor. Small pages exercise pagination even on an otherwise empty database.

This is an HTTP acceptance check. MQTT retry acknowledgment followed by an unchanged
GET does not independently prove that the retry has left the broker queue. Database
and Telegraf integration tests additionally verify quarantine, duplicate prevention,
permissions, and concurrent writers. Dashboard browser login and rendering are
covered separately; this command does not exercise a browser.

## Exercise both Node-RED write modes

Set `INGESTION_BACKEND=api` in `deploy/.env`, then run:

```sh
make dev
make test-e2e E2E_ARGS='--node-red-backend api'
```

Change that setting to `mqtt` and rerun:

```sh
make dev
make test-e2e E2E_ARGS='--node-red-backend mqtt'
```

The optional flag asserts the expected response mode, so testing an accidentally
unchanged API route cannot pass as an MQTT test. Without the flag, either supported
mode is accepted. The Go endpoint is tested in every run.

## Other endpoints and troubleshooting

For a separately configured test deployment:

```sh
make test-e2e E2E_ARGS='--api-url https://api.test.example --node-red-url https://flows.test.example --api-key-file /secure/test-api-key --cacert /secure/test-ca.crt --timeout 60'
```

Use `--system-ca` instead of `--cacert` for certificates trusted by the operating
system. Plain HTTP is supported only on loopback origins for temporary local
services. Full options: `python3 deploy/scripts/e2e.py --help`.

Failures exit nonzero. The default per-request timeout is five seconds, with a
30-second visibility deadline per observation. A curl transport failure means the
endpoint could not be reached or TLS verification failed. Check service status and
the CA path. An observation visibility timeout after `202` means the broker accepted
the write but storage was not verified; inspect Node-RED, Mosquitto, Telegraf, and
`sensor.rejected_messages`. Do not treat timeout as proof that nothing was stored.

## Automated and local verification

The CI `end-to-end` matrix starts the full Compose stack with fresh credentials
and volumes for each backend, runs the same command against Caddy HTTPS endpoints,
and removes only that CI stack afterward. It uses the repository's pinned
TimescaleDB image. Its result is separate from the unit and database jobs.

The verifier itself has regression tests under `deploy/scripts/tests/test_e2e.py`,
run by `make test-deploy`. They exercise eventual visibility, repeatability,
authentication failures, lost writes, corrupted reads, broken pagination, secret
handling, and real curl transport against a controlled HTTP peer.

Local acceptance passed against actual Go API, Node-RED, Mosquitto, and Telegraf
processes with PostgreSQL 16 for both backend selections: 24 observations were
verified in total. That run used loopback HTTP and did not exercise Caddy or the
TimescaleDB extension. Full Compose CI execution remains pending until the changes
are committed and run in GitHub Actions.
