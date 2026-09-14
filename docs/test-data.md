# Development test data

From the repository root, run:

```sh
make dev-test-data
```

This runs `make dev` first, preserving existing credentials, flows and volumes.
It then uses authenticated HTTPS requests to load observations through **both**
Node-RED (`flows.localhost`) and the direct Go API (`api.localhost`). No SQL inserts
or manual evaluator calls are used. Python 3.9+ and curl are required in addition
to the normal development prerequisites. TLS verification uses the generated
Caddy CA. Keys are read from secret files and passed to curl over stdin.
The loader only supports the default local development API hostnames.

## Historical dataset

Version 4 generates **580 observations**: four sensors with 145 readings each,
ten minutes apart over 24 hours ending at the current UTC hour.

| Sensor | Behaviour | Write route |
| --- | --- | --- |
| `WL-002` | Gradual rise from 1.4 to 2.4 m; continues into level warning | Direct API |
| `WL-003` | Gradual rise from 2.1 to 3.4 m; continues into level critical | Node-RED |
| `WL-004` | Small fluctuations from 0.7 to 0.8 m; continues into rise warning | Direct API |
| `WL-005` | Small fluctuations from 0.6 to 0.8 m; continues into rise critical | Node-RED |

All observations have `synthetic: true` and a dataset version in metadata. They
use the `demo-test-data` gateway, metres, and canonical `water-level` sensor type.
Every sensor has a numeric location ID: 9000–9003 for historical stations and
the same 9000–9003 for their live alert readings. Quickstart uses location 9004. Historical stations 9000 and 9002 also
have the same example GPS coordinates on historical and live readings. These
are illustrative locations, not claims about actual monitoring stations.

Each route receives single and batch requests, with batches of at most 100.
Every observation is read back through **both** endpoints and compared, including
its stored sequence and receipt time. In MQTT mode, HTTP 202 is only acceptance;
the loader waits for actual database visibility before reporting success.

The generated payload is saved to the Git-ignored
`deploy/test-data/observations.json`. UUIDs are deterministic for the dataset
version and end timestamp. Each invocation replaces the loader-owned fixtures,
so repeated runs keep four test sensors, plus the setup's `quickstart` sensor
(displayed as `WL-001`): **five stations total** in a standard development stack.
Additional operator sensors remain visible.

Before loading, `reset_test_data.sql` removes observations, rules, rule versions,
alert history and notification outbox entries belonging to these reserved test
fixtures. It also removes legacy `demo-*` test datasets and per-run alert sensors.
The reset is transactional, serialized with the evaluator, and refuses to proceed
if reserved sensor IDs contain non-fixture observations or unrelated rules.
It checks the gateway, synthetic marker and recognized dataset version. Quickstart
and unrelated operator data are retained. Do not use `WL-002`–`WL-005` or
`dev-test-{level,rise}-{warning,critical}` for operator-managed data or rules.
This development-only cleanup uses Docker/psql; all new observations and rule
parameters still enter through the HTTP endpoints. Export test history first if
you need to retain it across runs.

For an exact replay, use the end timestamp printed by the loader:

```sh
make dev-test-data TEST_DATA_ARGS='--at 2026-09-14T12:00:00Z'
```

Use a timestamp in the past or present, including its timezone. To load an already
running development stack without repeating setup:

```sh
python3 deploy/scripts/dev_test_data.py --at 2026-09-14T12:00:00Z
```

## Real alert scenarios

After the history is verified, each invocation creates four fixture rules and six
fresh observations on the **same four historical sensors**. The alert admin API
configures level warning on `WL-002`, level critical on `WL-003`, rise warning on
`WL-004`, and rise critical on `WL-005`. Rule IDs are stable; the run identifier
is retained only in observation metadata and the exported report filename.
The reset prevents previous live readings from becoming a new run's rise baseline.
If the history ends less than 61 seconds ago, the loader waits for it to leave
the live rise window before submitting the alert scenarios.

Common demonstration parameters are:

| Parameter | Value |
| --- | --- |
| Level warning / critical | 2 / 3 m |
| Rise warning / critical | 0.1 / 0.2 m per 60 seconds |
| Rise window / minimum separation | 60 / 1 seconds |
| Level / rise hysteresis | 0.05 m / 0.02 m per 60 seconds |
| Hold / stale duration | 0 / 3600 seconds |

Fresh level readings of 2.4 and 3.4 m create warning and critical conditions.
Each historical series ends at exactly its first live value, with matching location,
GPS coordinates (where present), unit and ingestion route. The history ends at the
chosen anchor; the later live sample represents the same level after that gap.
No intermediate observations are fabricated.
The two rise scenarios continue from their historical endpoints of 0.8 m. After at least two seconds, their next values
are calculated from actual elapsed time to produce rates of 0.15 and 0.3 m per
minute. Warning and critical samples use different write routes. This short
baseline is deliberate for a fast functional demonstration; it is not a
recommended field configuration.

The loader waits for the **scheduled PostgreSQL evaluator** and requires all four
active conditions with the expected severities, and rejects additional active
conditions on those four fixture rules. It also checks each alert's event
history for the submitted observation ID. The default alert wait is 120 seconds;
for a slower configured scheduler, increase it, for example:

```sh
make dev-test-data TEST_DATA_ARGS='--alert-timeout 600'
```

Successful runs save rules, six live readings, and verified alerts to
`deploy/test-data/alerts-<run>.json`. Historical observations alone do not recreate
past alert events: the live evaluator intentionally skips old readings. The
separate fresh scenarios establish alert behaviour without changing that policy.

The fixtures remain available until the next load. Without new readings, enabled
rules can later produce stale-data alerts. Configured development webhooks can
receive these actual alert events; use development recipients.

## Viewing and failures

Open Grafana's **Water-level monitoring** dashboard with a time range covering the
chosen anchor (normally Last 24 hours). It shows historical series, recent
observations, active alerts and evaluator health. Filter or identify demo alerts
by their `WL-002` through `WL-005` station IDs. The Node-RED dashboard shows recent committed records.

Any rejected write, corrupt read-back, missing observation, unexpected route
status, missing alert, or missing alert evidence exits nonzero. Partial data and
rules are retained for diagnosis. Replaying the same historical anchor is safe;
the next invocation replaces the fixture set before loading. Concurrent invocations
against the same working directory are not supported because they share the
export file.

`make test-deploy` covers deterministic generation, temporal spacing, scenario
behaviour, single/batch requests, retries, asynchronous visibility, corruption,
missing observations, rule parameters, and alert-evidence checks with controlled
peers. These unit tests do not substitute for real ingestion. The Compose CI
matrix runs `make dev-test-data` with both Node-RED backends (`api` and `mqtt`) and
requires successful database read-back and scheduled alerts.

## Grafana station labels

Grafana uses `WL-001` for the setup's `quickstart` sensor and the actual IDs
`WL-002`–`WL-005` for test observations and alerts. Historical and live data now
share identities and location IDs. The former `WL-<run>-007` style represented
separate test runs, not different station types. Legacy fixtures are removed by
the next loader run. Other sensor IDs are displayed unchanged.

The [monitoring dashboard guide](monitoring-dashboard.md) explains the four
current/limit cards, sensor selection, freshness and normalized rise rates.

`make test-data-reset` verifies fixture cleanup and protection of unrelated data
in rolled-back PostgreSQL transactions. CI runs it for both ingestion backends.

Version 4 replaces the mismatched version 3 histories, which previously jumped
to unrelated levels when live alert samples began. Regression tests compare the
last historical and first live samples and preserve station attributes.
