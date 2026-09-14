# Contributing

Run commands from the DanuBaaS repository root. The Go module is
`github.com/AndreasWillibaldWeber/DanuBaaS`; the executable remains `sensor-api`.
Keep the existing Compose project names (`dbe-sensors` and
`dbe-sensors-production`) when upgrading installations so named volumes are reused.

Keep transport, domain validation, and persistence separate. Prefer a small change
with explicit behavior over a framework or abstraction without a current consumer.

Before submitting a change, run `make test vet` and the relevant integration tests.
Changes to deployment scripts also require `make test-deploy`; Node-RED changes
require `npm test --prefix deploy/nodered` and the relevant runtime integration tests.
Document any public contract change in `docs/openapi.yaml` and `README.md`. Changes
to ingestion semantics require tests for retries, partial failure, and concurrency.
Use a new migration for schema changes. Never rewrite an applied migration.

Keep credentials, raw production measurements, personal data, generated binaries,
and local test databases out of commits. Dependencies must remain pinned by
`go.sum`; review dependency upgrades and rerun the tests before merging them.

A useful change description states the triggering problem, resulting behavior,
verification, and any migration or operational impact. Do not claim a check passed
when it was skipped or could not run.
