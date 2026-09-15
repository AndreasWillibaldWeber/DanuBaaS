# Optional observation locations

The Go API, Node-RED REST endpoint, and MQTT/Telegraf path accept the same optional
location fields. They belong to each immutable observation, so a moving sensor can
report a different location with each new observation ID.

| Field | Accepted values | Storage |
| --- | --- | --- |
| `lon_lat` | `[longitude, latitude]` in WGS84 decimal degrees; longitude −180…180 and latitude −90…90 | Nullable `double precision` columns `longitude` and `latitude` |
| `location_id` | Integer JSON literal from 0 to 9007199254740991 | Nullable `bigint` column `location_id` |

Both fields may be omitted, null, supplied individually, or supplied together.
`[0, 0]` and location ID `0` are valid values. A supplied pair must have exactly two
finite numbers: `[16.3, null]`, `[16.3]`, strings, and out-of-range coordinates are
invalid. The ID bound preserves exact integers through Node-RED's JavaScript JSON
handling. This is a numeric external identifier; there is no location registry,
foreign key, ID-to-coordinate lookup, or validation that an ID matches a coordinate.

The report uses `lon-lat`; the canonical API uses `lon_lat` to match its other
snake_case fields. Legacy senders must map their field names and units before
submission. Arbitrary source-specific fields still belong in `metadata`.

## Send and read locations

After `make dev`, save the following observation as `value.json` in the repository
root. It uses synthetic coordinates and a demo location ID:

```json
{
  "id": "12345678-1234-4234-8234-123456789101",
  "sensor_id": "groundwater-01",
  "sensor_type": "water-level:ground-water",
  "timestamp": "2026-09-14T10:00:00Z",
  "value": 1.42,
  "unit": "m",
  "lon_lat": [16.3738, 48.2082],
  "location_id": 7
}
```

Send it through Node-RED, then read it by ID or as part of a list:

```sh
API_KEY="$(cat deploy/secrets/api_key)"
curl --fail-with-body --cacert deploy/certs/caddy-root.crt \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  --data-binary @value.json https://flows.localhost/api/v1/values

curl --fail-with-body --cacert deploy/certs/caddy-root.crt \
  -H "X-API-Key: $API_KEY" \
  'https://flows.localhost/api/v1/values?id=12345678-1234-4234-8234-123456789101'

curl --fail-with-body --cacert deploy/certs/caddy-root.crt \
  -H "X-API-Key: $API_KEY" 'https://flows.localhost/api/v1/values?limit=100'
```

Replace `flows.localhost` with `api.localhost` to use the Go API directly.
For other deployment settings, follow [the deployment examples](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/deploy/README.md#demonstrate-the-api).

For an unknown location, omit both fields or set each to `null` in the observation:
`"lon_lat": null` and `"location_id": null`.

POST accepts one object or an array, including batches mixing known and unknown
locations. GET by observation ID and paginated GET return supplied locations.
Missing and null fields are omitted from canonical responses, with SQL NULL in
all corresponding measurement columns. They are never replaced with zero.

Use a fresh UUID if you have already submitted an example with different content.
Retain observation IDs and content on retry. Omitted and null locations are
interchangeable, including retries across ingestion routes. Adding, removing, or
changing a location on an already stored ID changes the observation: the Go API
returns `409`; the MQTT database adapter quarantines the whole document. An invalid
final observation also prevents partial batch storage. In MQTT mode, Node-RED's
`202` acknowledges the broker; poll GET to confirm database visibility.

## Query and upgrade

The Grafana recent-observations table includes `longitude`, `latitude`, and
`location_id`; missing locations appear empty. Node-RED's table uses the Go read
API and receives the optional fields. Neither dashboard performs location lookup
or provides a geographic map.

```sql
SELECT time, sensor_id, longitude, latitude, location_id, value, unit
FROM sensor.dashboard_values
ORDER BY time DESC
LIMIT 100;
```

Apply migration `003_optional_locations.sql` with the migration service before
running the rebuilt API and Node-RED images; follow the
[existing-deployment upgrade procedure](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/deploy/README.md#upgrade-an-existing-deployment).
Do not delete volumes or edit previously applied migrations. The new columns are
nullable, and existing observations keep their original IDs, sequences, timestamps,
and canonical JSON. Existing location fields inside `metadata` remain there and
are not automatically copied into the new fields. Replaying those observations
with omitted or null top-level locations remains valid. Adding location data to
the same UUID would conflict; any correction needs an explicit data-management
policy outside this append-only demo API.
