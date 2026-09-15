# Water-level monitoring dashboard

Open Grafana → Sensors → **Water-level monitoring**. The dashboard is provisioned
by `make dev`, `make dev-test-data`, and production Compose setup.

## Four monitoring cards

The top row uses Grafana’s built-in Canvas panels for explicit label placement
and contains warning and critical cards for water level, followed by
warning and critical cards for rise rate. Each card shows the current value on
the **left** and the corresponding configured limit on the **right**, with the
station label on a second line beneath “Current” or “Limit”. Orange identifies warning cards; red
identifies critical cards. These colours identify threshold categories regardless
of whether the value exceeds the limit; the active-alert table records actual
alert conditions, including holds and hysteresis.

Cards use the latest valid, non-future canonical `water-level` reading in metres
from sensors with enabled rules. Freshness is determined by each rule's
`stale_seconds`. Readings preceding the latest rule change are not treated as
current monitoring data. The cards do not replace the PostgreSQL evaluator:
they calculate a current measurement comparison, while alert state remains the
evaluator's responsibility.

Rise rates use the earliest valid baseline in the rule's `rise_window_seconds`,
separated by at least `rise_min_seconds`. Rates and thresholds are converted to
**metres per minute**, so rules with different `rise_period_seconds` can be
compared. The stored rule parameters remain unchanged. Migration `005` appends
observation identity to the reporting view for deterministic timestamp tie handling.

Sensors without enabled rules show an **em dash (—)**. Configured sensors with
missing or stale readings or absent rise baselines show **-.-- m** or **-.-- m/min**,
with the reason beneath the station name. Their configured limits remain visible. A missing reading is never converted
to zero. For disabled or unconfigured sensors, consult Monitoring status and the
rule API before expecting card values.

## Sensor selection and All

The **Monitored sensor** dropdown supports one sensor or All.
It lists known water-level sensors and configured rule sensors, using station
labels while retaining original IDs as query values. The same selection filters
all four cards, the time series, recent observations, active alerts, and monitoring
status. Queries escape selected IDs with Grafana's `sqlstring` formatting.

For All, each card chooses the **highest fresh current
value among selected sensors with enabled rules**, then takes the limit from
that same sensor's rule. It never combines one sensor's reading with another
sensor's threshold. Water-level and rise-rate winners may differ. Equal values
are resolved by sensor ID. When no candidate has a current value, the card shows
the appropriate missing-value placeholder; if an enabled rule exists, its station
and limit are still shown.

The historical time picker controls the combined value/range chart and recent observations. Cards,
unresolved alerts, and monitoring status describe **current** conditions and
remain independent of that historical window. Sensor filtering applies to both.
The evaluator heartbeat is global, shown alongside each selected rule's status;
changing sensors does not imply separate scheduler instances.

## Maintenance and verification

`deploy/grafana/build_dashboard.py` holds the shared SQL and panel definitions.
After editing it, regenerate the checked-in provisioning file:

```sh
python3 deploy/grafana/build_dashboard.py
make test-deploy
make test-grafana test-data-reset
```

`make test-grafana` requires the running development database. It verifies paired
limits, maximum selection, different rate periods, freshness, future readings,
missing baselines, disabled and absent rules, empty selection, escaped IDs, and
timestamp ties using temporary PostgreSQL fixtures. These fixtures are rolled
back. Every real panel and the dropdown are also executed with `grafana_reader`
privileges. CI runs this check in both ingestion-backend Compose jobs.

The dashboard template remains checked in. The read-only `grafana-dashboard`
service renders native per-sensor band overrides into a shared volume at runtime. Normal migrations and existing read-only grants apply automatically.
For an existing deployment, run `make dev` (or the documented production upgrade
procedure) to apply migration `005` before using the updated dashboard. Grafana's
file provisioning loads dashboard changes; refresh the browser afterwards.

Canvas reference: [Grafana documentation](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/canvas/).

See [measured ranges](measurement-ranges.md) for minimum/maximum chart bands and
the value and bounds captured when an alert opens.
