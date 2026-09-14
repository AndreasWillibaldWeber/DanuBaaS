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

Version 1 generates **580 observations**: four sensors with 145 readings each,
ten minutes apart over 24 hours ending at the current UTC hour.

| Sensor | Behaviour | Write route |
| --- | --- | --- |
| `demo-stable` | Approximately 1.2 m with small fluctuations | Direct API |
| `demo-fluctuating` | Approximately 1.25–2.05 m | Node-RED |
| `demo-elevated` | Gradual rise from 2.1 to 3.35 m | Direct API |
| `demo-rapid-rise` | 0.9 m, then a 1.2 m rise in the last 30 minutes | Node-RED |

All observations have `synthetic: true` and a dataset version in metadata. They
use the `demo-test-data` gateway, metres, and canonical `water-level` sensor type.
Some sensors use example GPS coordinates; others use numeric location IDs. These
are illustrative locations, not claims about actual monitoring stations.

Each route receives single and batch requests, with batches of at most 100.
Every observation is read back through **both** endpoints and compared, including
its stored sequence and receipt time. In MQTT mode, HTTP 202 is only acceptance;
the loader waits for actual database visibility before reporting success.

The generated payload is saved to the Git-ignored
`deploy/test-data/observations.json`. UUIDs are deterministic for the dataset
version and end timestamp. Rerunning within the same UTC hour replays the same
historical readings without duplicating them. A new end timestamp adds another
580 readings. Overlapping runs remain distinct datasets, so charts may contain
multiple readings at the same time; use a fresh disposable environment if this
is unsuitable for your experiment. Nothing is automatically deleted.

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

After the history is verified, every invocation creates **four new rules and six
fresh observations**, using unique `demo-alert-<run>-...` sensor/rule IDs. The
separate alert administrative key is used only for rule creation. Existing rules,
including `quickstart` and operator edits, are never replaced. Each run is isolated
from previous rise-rate baselines.

Common demonstration parameters are:

| Parameter | Value |
| --- | --- |
| Level warning / critical | 2 / 3 m |
| Rise warning / critical | 0.1 / 0.2 m per 60 seconds |
| Rise window / minimum separation | 60 / 1 seconds |
| Level / rise hysteresis | 0.05 m / 0.02 m per 60 seconds |
| Hold / stale duration | 0 / 3600 seconds |

Fresh level readings of 2.4 and 3.4 m create warning and critical conditions.
Two other sensors start at 0.8 m. After at least two seconds, their next values
are calculated from actual elapsed time to produce rates of 0.15 and 0.3 m per
minute. Warning and critical samples use different write routes. This short
baseline is deliberate for a fast functional demonstration; it is not a
recommended field configuration.

The loader waits for the **scheduled PostgreSQL evaluator** and requires all four
active conditions with the expected severities. It also checks each alert's event
history for the submitted observation ID. The default alert wait is 120 seconds;
for a slower configured scheduler, increase it, for example:

```sh
make dev-test-data TEST_DATA_ARGS='--alert-timeout 600'
```

Successful runs save rules, six live readings, and verified alerts to
`deploy/test-data/alerts-<run>.json`. Historical observations alone do not recreate
past alert events: the live evaluator intentionally skips old readings. The
separate fresh scenarios establish alert behaviour without changing that policy.

The rules, observations, alert history and notification outbox entries remain in
the database. Every invocation adds another four demo rules and six live readings,
even when historical data is replayed. Without further readings, enabled rules
can later produce stale-data alerts. If a development webhook is configured,
these genuine alert events can be delivered to it. Do not point development
notifications at operational recipients. Demo thresholds require site calibration
before operational use.

## Viewing and failures

Open Grafana's **Sensor observations** dashboard with a time range covering the
chosen anchor (normally Last 24 hours). It shows historical series, recent
observations, active alerts and evaluator health. Filter or identify demo alerts
by the `demo-alert-` prefix. The Node-RED dashboard shows recent committed records.

Any rejected write, corrupt read-back, missing observation, unexpected route
status, missing alert, or missing alert evidence exits nonzero. Partial data and
rules are retained for diagnosis. Replaying the same historical anchor is safe;
the live alert portion always creates a new isolated run. Concurrent invocations
against the same working directory are not supported because they share the
export file.

`make test-deploy` covers deterministic generation, temporal spacing, scenario
behaviour, single/batch requests, retries, asynchronous visibility, corruption,
missing observations, rule parameters, and alert-evidence checks with controlled
peers. These unit tests do not substitute for real ingestion. The Compose CI
matrix runs `make dev-test-data` with both Node-RED backends (`api` and `mqtt`) and
requires successful database read-back and scheduled alerts.
