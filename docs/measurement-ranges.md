# Measured minimum and maximum

An observation's `value` is the reported water level (for example, an average of
sensor samples). Optional `metadata.minimum` and `metadata.maximum` describe the
measured extrema over the same sampling interval, in the observation's unit:

```json
{"value":1.42,"unit":"m","metadata":{"minimum":1.39,"maximum":1.47}}
```

Supply both bounds or omit both. Both must be finite JSON numbers satisfying
`minimum <= value <= maximum`. Equal bounds are allowed. Nulls, numeric strings,
partial ranges and reversed or out-of-range bounds are rejected. Other metadata
remains supported. A legacy `deviation` number is preserved but is not interpreted
as symmetric bounds or a confidence interval.

The API rejects invalid ranges with HTTP 422 before writing a batch. The database
also enforces the contract, including MQTT/Telegraf ingestion: an invalid member
quarantines the whole MQTT document. Existing observations are not rewritten.
Migration 006 exposes valid bounds as `minimum` and `maximum` in
`sensor.dashboard_values`; absent or malformed legacy ranges appear as NULL.
The NOT VALID constraint preserves legacy rows while validating all new writes.

## Grafana

The **Water levels** plot combines value lines and translucent measured
minimum–maximum bands for every selected sensor. Monitored sensor selects one
sensor or All. Each sensor keeps its colour when filtering. Only value series
appear in the legend; the tooltip includes that sensor's value and bounds.
Readings without bounds retain the value line; missing bounds are not bridged.

Grafana's native `fillBelowTo` option requires exact series names. The
`grafana-dashboard` service discovers sensors through the existing read-only
Grafana database role and renders an explicit upper/lower pairing for each ID.
It polls every 10 seconds and atomically updates the shared `grafana_dashboards`
volume only when the template or sensor roster changes. Grafana polls that volume
every 10 seconds. After adding a new sensor, allow those polls to complete and
reload the dashboard to obtain its new variable options and band configuration.
The service needs no Grafana administrative credentials or external network access.

Source configuration remains in `deploy/grafana/build_dashboard.py` and its
checked-in JSON template. Do not edit the generated volume file. On a failed
refresh the last successful dashboard is retained; a heartbeat older than 35
seconds makes the service unhealthy. Both `make dev` and production Compose
start it before Grafana. It runs as the image's non-root user with a read-only
root filesystem and write access only to the generated-dashboard volume.

The Active alerts table adds **value_at_trigger**, **minimum_at_trigger** and
**maximum_at_trigger** between severity and opened_at, in metres. These values
come from the original opening event, not the latest sensor reading. Later
escalations retain their own event evidence but do not overwrite the opening
snapshot shown here. The displayed severity may therefore differ from the initial
severity. Stale-data alerts have no triggering measurement and show dashes.
Existing alert events without bounds also show dashes for those fields.

The evaluator copies measured bounds into event evidence when the event is
recorded. This preserves evidence after measurement retention removes the source
row. Threshold evaluation continues to use `value`, not either bound; this change
does not alter the alert policy.

## Setup and verification

`make dev` and `make dev-test-data` apply migration 006 and provision the panels.
Dataset version 6 includes asymmetric bounds (value minus 0.04 m, value plus
0.06 m) on all four historical/live test sensors. Quickstart still demonstrates a
reading without bounds. Live alert samples recalculate their bounds as they rise.

Run `go test ./...`, `make test-deploy`, and, with the development database running,
`make test-grafana test-ranges test-data-reset`. The range SQL check rolls back its
fixtures and verifies MQTT ingestion, invalid batch quarantine, omitted bounds,
and preservation of opening evidence after later readings, escalation and
measurement deletion. CI runs it with both ingestion backends.
