> Update: migration `004` now implements configurable warning/critical water-level
> and rise-rate rules, stale-data detection, persistent history, acknowledgment and
> optional webhook delivery. See [alerting.md](alerting.md). Statements below that
> describe these features as absent refer to the earlier assessment. Calibration,
> intervention tracking and field evaluation remain outstanding.

# Report–implementation alignment review

Reviewed the active LaTeX chapters in `DBE_Digital_Project_Workshop`, both PDF
architecture diagrams, the supplied assessment brief, and the current Go,
Node-RED, SQL, Compose, dashboard, and bootstrap sources. This is a source and
documentation comparison, not a new deployment test. The report itself is unchanged.

## Management recommendation

Present the work as an implemented data-platform prototype that supports a staged
investment decision. The business problem is fragmented and insufficiently local
water-level information when staff must prioritise countermeasures. The proposed
value is a shared, timely information base and reduced dependence on separate
proprietary sensor platforms. Actual improvements in decisions, maintenance costs,
and flood damage remain hypotheses to evaluate.

Suggested executive message:

> The project provides a common software foundation for collecting and displaying
> local water-level observations. It combines established open-source components
> with a small custom integration layer and supports multiple sensor interfaces.
> The next decision is to authorise a bounded validation pilot using real
> measurements and operator feedback. The pilot should establish data quality,
> operational usefulness, and support effort before a wider deployment is funded.
> Financial benefits and the effectiveness of flood-warning rules will be assessed
> against documented baselines.

The funding or resource request should identify an owner, a proposed pilot scope,
a cost ceiling, and decision dates. These are not specified in the current report;
do not invent committed budgets or schedules. Separate success gates:

| Gate | Evidence required before proceeding |
| --- | --- |
| Reproducible technical demonstration | Start the complete stack; ingest and retrieve representative observations through both routes; show authenticated dashboards and a successful backup/restore. |
| Operational pilot | Use genuine data from selected locations; record gaps, erroneous readings, data latency, operator feedback, and maintenance effort. Agree acceptable targets with stakeholders. |
| Warning-system trial | Implement and calibrate the chosen level/rise-rate rules, missing-data behavior, and alarm/action history; evaluate false alarms, missed events, and useful warning time. |
| Wider deployment | Review total ownership cost, operating responsibility, security/backup arrangements, and evidence of improved decisions. Evaluate avoided damage over a suitable period with explicit uncertainty. |

For management, the main report should explain the problem, proposed value,
available evidence, limitations, and next decision. Put API details, exact topic
names, setup commands, and most test output in an appendix. Keep enough architecture
and data-quality evidence in the main text to satisfy the CRISP-DM assessment brief.

## Overall assessment

The backend infrastructure concept is substantially aligned: Docker Compose,
Caddy, Node-RED, Mosquitto, Telegraf, TimescaleDB, and Grafana are present. The
report's Node-RED → Mosquitto → Telegraf → TimescaleDB route is implemented when
`INGESTION_BACKEND=mqtt`. The default is the additional Go API route.

Functional alignment is partial. Collection, canonical validation, storage,
retrieval, and basic dashboards exist. Configurable flood indicators, rate-of-rise
alarms, operational response records, and field evaluation do not. Several report
statements also describe an earlier interface and security design. A numerical
compliance percentage would imply weights and acceptance criteria that the report
does not define.

The assessment brief permits a feasibility study and planned solution. Therefore,
unimplemented features can remain in the report as explicitly proposed work.
They cannot be presented as delivered features or evaluated outcomes.

## Alignment matrix

| Area | Current alignment | Required report revision |
| --- | --- | --- |
| Hardware outside the backend scope | Aligned | Retain chapter 2's scope boundary; label radio reliability and gateway behavior as upstream assumptions/evidence, not backend test results. |
| Core open-source services in Docker | Aligned at component level | Describe the supplied Compose deployment and its actual network boundaries. |
| REST → Node-RED → MQTT → Telegraf → database | Implemented as a selectable route | Explain both `INGESTION_BACKEND` values, including the default `api` route. |
| Direct MQTT ingestion | Different routing | External publishers use `raw/v1/<gateway>/<sensor>`; Node-RED still validates/routes these messages. Only Node-RED publishes `normalized/v1/batch`, which Telegraf consumes. The supplied ACLs do not allow a gateway to bypass Node-RED onto the normalized topic. |
| Custom API | Different contract and implementation | Document Go standard-library HTTP plus pgx, GET/POST `/api/v1/values`, internal port 8080, API keys, and paginated list reads. |
| Data dictionary | Incompatible as an API payload | Distinguish the proposed gateway wire format from the implemented canonical observation format; supply a mapping and a real example. No legacy-format converter is supplied. |
| JSON preservation | Partly aligned | Describe canonical JSONB and metadata. Original whitespace, key order, numeric spelling, and original packet bytes are not preserved automatically. |
| Dashboards | Different from chapter 5's desktop preference | Describe browser-based Grafana and FlowFuse Dashboard on Node-RED. There is no separate desktop application or location-decoding file. |
| Authentication | Different for browser users | Document Caddy Basic Auth plus the separate application login; machine REST uses an API key and MQTT uses broker credentials/ACLs. |
| Alarms and operational evaluation | Not implemented | Keep configurable thresholds, rise-rate rules, acknowledgments, intervention history, and the two-year evaluation as planned work, or implement and evaluate them before claiming delivery. |
| Compression and rollups | Not configured | Remove these from the implemented database box or mark them as future options. A hypertable alone does not demonstrate these features are enabled. |
| Host hardening | Not provisioned here | Label Debian/VPS, nftables, sshd, and fail2ban as host requirements or a deployment proposal. Compose does not install/configure them. |
| Real-world data and outcomes | Not established by this repository | Add actual measurements, provenance, and observed quality problems. Test fixtures and the `make dev` sample are synthetic engineering test data. |

## Chapter-by-chapter changes

### 1. Management Summary

`content/01-management-summary.tex`, lines 11–23, still contains assignment
instructions and a dummy figure. Replace them with the problem, implemented PoC,
observed results, limitations, and a sourced cost discussion. Do not claim flood
damage reduction, forecast accuracy, or production readiness from passing unit tests.

Suggested scope wording:

> This work develops a proof-of-concept backend for collecting, validating,
> storing, and visualising water-level observations. It supports a synchronous
> Go API ingestion route and an alternative MQTT/Telegraf route selected in
> Node-RED. Configurable flood-warning rules and the evaluation of operational
> countermeasures remain subsequent project stages. The prototype does not yet
> demonstrate a reduction in flood damage.

### 2. Business Understanding

Retain the problem, stakeholders, and hardware exclusion. In
`content/02-business-understanding.tex`, line 68, separate the long-term objective
from the implemented PoC's acceptance criteria. Appropriate PoC criteria include
valid observation round trips, complete-batch handling, retry identity, rejection
of unauthorized requests, and accessible authenticated dashboards.

Keep water-level thresholds, rate-of-rise indicators, first-year calibration, and
second-year operational evaluation as project goals. Describe how future success
will be measured. Attribute the financial estimates to a dated source or named
stakeholder estimate; implementation tests cannot validate those estimates.

### 3. Data Understanding

Revise `content/03-data-understanding.tex`, lines 25–32, to describe actual ingress:

- HTTPS requests reach Caddy before the Go or Node-RED endpoint.
- Node-RED selects either the Go writer or MQTT/Telegraf writer.
- MQTT clients publish canonical observations on raw topics; the current flow
  does not convert the report's original field names automatically.
- The Go path returns success after commit. The Node-RED MQTT path returns `202`
  after broker acknowledgment; database visibility must be checked separately.
- Preserve event IDs and payloads on retry. A gateway must not equate MQTT PUBACK
  or HTTP `202` with a durable database commit. Its actual buffering behavior
  remains outside this backend's verified scope.

Replace or qualify the data dictionary at lines 42–53:

| Report field | Implemented representation |
| --- | --- |
| Numeric packet `id` | Preserve as a chosen metadata field, e.g. `metadata.packet_id`; use a separate lowercase UUID string as observation `id`. |
| Numeric `gid`, `sid` | String identifiers `gateway_id`, `sensor_id`; convert explicitly in any upstream adapter. |
| `sensor-type` | `sensor_type` |
| `time-stamp` | `timestamp`: RFC3339 with timezone, at most microsecond precision; stored normalized to UTC. |
| `water-level` | Numeric `value` plus required `unit`, e.g. `m`. |
| `lon-lat` or numeric location ID | Top-level `lon_lat: [longitude, latitude]` and `location_id`; both independently optional/nullable. Typed storage and reporting columns exist; no registry lookup or location-decoding file is implemented. |
| `ref-to-zero`, `deviation`, `rssi`, `check-sum`, `hash` | Optional metadata fields. The names inside metadata must be agreed/documented; storing checksum/hash values does not verify them. |
| Not in report | Server-generated `sequence` and `received_at` in API responses; clients do not submit them as observation fields. |

The original dictionary may remain useful if titled **proposed gateway wire
format**. Add a separate **canonical backend contract** and make clear that the
mapping is specified, not an already implemented generic decoder.

Describe the actual storage objects:

- `sensor.events`: global UUID uniqueness, canonical payload, receipt time, sequence.
- `sensor.measurements`: event-time hypertable with typed measurements, nullable longitude/latitude and location ID, and metadata.
- `sensor.dashboard_values`: read-only reporting view used by Grafana.
- `sensor.mqtt_ingest`: transient INSERT/COPY interface, consumed by a SQL trigger.
- `sensor.rejected_messages`: rejected MQTT documents and error reasons.

Explain transactional batches and cross-route retries. MQTT conflicts are
quarantined asynchronously; direct API conflicts return `409`.

Expand the quality discussion at lines 63–69 with actual observations and counts:
missing values, duplicates, timestamp problems, noise, gaps, and calibration issues.
The backend validates shape and finite numeric values; it does not implement the
described sensor averaging/outlier removal or verify physical calibration. Define
whether `value` means distance, relative water level, or elevation against a datum.
Irregular, change-triggered reporting also matters: a quiet sensor is not necessarily
a failed sensor, and rise rates require actual elapsed time between observations.

### 4. Solution Concept & Modeling

Replace the placeholder instructions at
`content/04-solution-concept-modeling.tex`, lines 9–15. Add the two write paths,
read paths, authentication boundaries, schema summary, and delivery semantics.
Include real dashboard screenshots after a successful deployment, or clearly
label any mockups as proposed visuals.

The supplied dashboards are a Grafana water-level chart/recent-record table and a
Node-RED table of the latest 100 received records. No flood alarm rules or maps are
provisioned. If a rule example is needed for the conceptual study, label it as
proposed: level above a per-site threshold, or change in level divided by elapsed
time above a per-site rise-rate threshold. Specify missing-data behavior and
calibration plans without inventing validated thresholds or detection results.

### 5. Evaluation & Transfer

Revise `content/05-evaluation-transfer.tex`, lines 18–20 and 31–32:

- Replace the desktop-dashboard preference with the chosen browser dashboards,
  or explicitly identify the desktop version as an abandoned alternative.
- Explain the actual username/password browser logins. If “code-based” meant
  API keys, restrict that statement to machine clients; no passwordless browser
  code-login flow is implemented.
- Remove the claim that a desktop app and decoding file resolve locations.
  No such component exists. The API accepts optional `lon_lat` and `location_id`,
  and Grafana's reporting view exposes typed longitude, latitude, and location ID.
  These fields do not resolve IDs to coordinates or provide a map dashboard.
- Replace the categorical privacy assurance with an explicit data-minimization
  policy and description of data actually collected, including authentication
  and operational data. The implementation does not prevent users from putting
  identifying information in metadata or optional location fields.
- Acknowledge custom maintenance work: Go API, Node-RED authentication/publisher,
  SQL ingestion trigger, and setup/preflight scripts. State why these additions
  were chosen despite the preference for standard software.
- Distinguish recorded component test results from deployment and field results.
  Full Docker/TimescaleDB deployment, live dashboard verification, restore tests,
  and a two-year field study are not established by the current workspace checks.
- Document `make dev`, `make prod-check`, and `make prod`. Production requires
  supplied secrets/certificates; a successful preflight is not proof of production
  suitability or end-to-end correctness.

Complete the empty Summary & Conclusion. Describe remaining operational work and
the evidence needed to assess benefits. Frame hosting/maintenance costs as dated,
sourced estimates; no hosting-price or cost-effectiveness verification was performed
as part of this source comparison.

## Specific diagram corrections

For `figures/architecture.pdf`:

1. Rename `RESET-API` to `REST API`; replace `GoLang + FastAPI + GORM` with
   `Go net/http + pgx`.
2. Replace `/api/ingest` with `/api/v1/values`. Show GET and POST, not only GET.
3. Change the API's internal port from 8000 to 8080. API-to-database access includes
   SELECT and INSERT; only Grafana is read-only. Port 8081 is private API health.
4. Add Node-RED → Go API as the alternative write route. Show Node-RED/dashboard
   reads through the Go API, and Grafana reads through its database view.
5. Replace `sensors/<site>/<device>` and `sensors/+/+` with raw and normalized
   topics and their actual routing. Label Telegraf as a consumer of whole JSON
   documents feeding the transactional SQL adapter, not an implemented unpivot stage.
6. Label the browser dashboards as Grafana and Node-RED/FlowFuse Dashboard.
   Show Caddy Basic Auth plus separate application authentication.
7. Remove or mark compression, rollups, and alerting as unconfigured future options.
8. Distinguish host assumptions from supplied containers. Show published HTTP 80,
   HTTPS 443, MQTT TLS 8883, and loopback-only editor 1880. SSH 22 is a host service,
   not a Compose-published port. Private HTTP/MQTT/PostgreSQL connections are not
   configured with TLS in this single-host demonstration.

For `figures/data-source.pdf`, add Caddy before Node-RED and show both selectable
routes, or caption the existing chain explicitly as the MQTT-mode detail view.
Do not present it as the only path or the default configuration.

## Academic evidence and completion

The supplied `50_Pruefungsleistung_DigiPro_V2.pdf` explicitly requires real data,
provenance evidence, and discussion of observed imperfections. It allows a
feasibility study; it does not require a finished operational flood-warning system.

The quick-start sample and automated test fixtures are useful software checks but
do not establish the real-data requirement. The hardware photos support the
hardware context, but the provided chapters do not show an analyzed measurement
sample. Include genuine upstream measurements or an appropriate documented real
dataset, capture dates, sensor/location context, and actual quality findings.
This does not require adding hardware implementation to the software scope.

Also complete the management summary, solution chapter, and conclusion; add the
requested AI-use disclosure in the appendix; remove dummy figures and unrelated
template material; review bibliography relevance and evidence for cost claims.
The active acknowledgements chapter currently contains only commented template
material. Keep the main text within the brief's 10-page limit and put detailed
API examples, logs, test output, and reproducibility instructions in the appendix.

## Changes that wording alone cannot deliver

If the report continues to claim a functioning flood-warning/response system,
implementation work is needed for configurable rules, rate-of-rise calculation,
stale-data handling, alert persistence/acknowledgment, and intervention records.
If it claims direct compatibility with the original gateway dictionary, an actual
adapter and representative real-payload tests are needed. If it claims completed
deployment, real data evaluation, restore reliability, or economic benefits,
corresponding execution and evidence are needed. Marking these as future work
aligns the report with the PoC; it does not complete the original operational goals.

Implementation references: [API contract](../README.md),
[schema migrations](../internal/postgres/migrations),
[Compose](../deploy/compose.yaml), [Node-RED flows](../deploy/nodered/flows.json),
[MQTT ACL](../deploy/mosquitto/acl), [Telegraf](../deploy/telegraf/telegraf.conf),
[Grafana dashboard](../deploy/grafana/dashboards/sensors.json),
[quality evidence and limitations](quality.md).
