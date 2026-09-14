-- Development-only fixture reset. Never truncate shared tables.
BEGIN;
SELECT pg_advisory_xact_lock(7349203); -- serialize with rule writes and evaluator
CREATE TEMP TABLE fixture_sensors(sensor_id text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO fixture_sensors VALUES ('WL-002'),('WL-003'),('WL-004'),('WL-005'),
 ('demo-stable'),('demo-fluctuating'),('demo-elevated'),('demo-rapid-rise');
INSERT INTO fixture_sensors
 SELECT DISTINCT payload->>'sensor_id' FROM sensor.events
 WHERE payload->>'sensor_id' ~ '^demo-alert-[0-9a-f]{12}-(level|rise)-(warning|critical)$'
 UNION SELECT sensor_id FROM configuration.alert_rules
 WHERE id=sensor_id AND sensor_id ~ '^demo-alert-[0-9a-f]{12}-(level|rise)-(warning|critical)$'
 ON CONFLICT DO NOTHING;
-- Refuse to repurpose an identity containing non-fixture observations or rules.
DO $$ BEGIN
 IF EXISTS (SELECT FROM sensor.events e JOIN fixture_sensors s ON s.sensor_id=e.payload->>'sensor_id'
 WHERE NOT coalesce(e.payload->>'gateway_id'='demo-test-data'
   AND e.payload->'metadata'->>'synthetic'='true'
   AND e.payload->'metadata'->>'dataset' IN
    ('dev-test-data-v1','dev-test-data-v2','dev-test-data-v3','dev-test-alerts-v1'),false))
 OR EXISTS (SELECT FROM configuration.alert_rules r JOIN fixture_sensors s USING(sensor_id)
 WHERE NOT (r.id=r.sensor_id AND r.id ~ '^demo-alert-[0-9a-f]{12}-(level|rise)-(warning|critical)$')
 AND NOT (r.id='dev-test-level-warning' AND r.sensor_id='WL-002')
 AND NOT (r.id='dev-test-level-critical' AND r.sensor_id='WL-003')
 AND NOT (r.id='dev-test-rise-warning' AND r.sensor_id='WL-004')
 AND NOT (r.id='dev-test-rise-critical' AND r.sensor_id='WL-005'))
 THEN RAISE EXCEPTION 'Reserved test sensor contains non-fixture data or rule; reset aborted'; END IF;
END $$;
CREATE TEMP TABLE fixture_rules ON COMMIT DROP AS
 SELECT id FROM configuration.alert_rules WHERE sensor_id IN (SELECT sensor_id FROM fixture_sensors);
CREATE TEMP TABLE fixture_alerts ON COMMIT DROP AS
 SELECT id FROM alerting.alerts WHERE rule_id IN (SELECT id FROM fixture_rules);
DELETE FROM alerting.notification_outbox WHERE event_id IN
 (SELECT id FROM alerting.events WHERE alert_id IN (SELECT id FROM fixture_alerts));
DELETE FROM alerting.events WHERE alert_id IN (SELECT id FROM fixture_alerts);
DELETE FROM alerting.alerts WHERE id IN (SELECT id FROM fixture_alerts);
DELETE FROM alerting.condition_state WHERE rule_id IN (SELECT id FROM fixture_rules);
DELETE FROM alerting.rule_state WHERE rule_id IN (SELECT id FROM fixture_rules);
DELETE FROM configuration.alert_rule_versions WHERE rule_id IN (SELECT id FROM fixture_rules);
DELETE FROM configuration.alert_rules WHERE id IN (SELECT id FROM fixture_rules);
DELETE FROM sensor.measurements WHERE sensor_id IN (SELECT sensor_id FROM fixture_sensors);
DELETE FROM sensor.events WHERE payload->>'sensor_id' IN (SELECT sensor_id FROM fixture_sensors);
COMMIT;
