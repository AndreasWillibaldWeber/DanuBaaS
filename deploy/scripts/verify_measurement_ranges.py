#!/usr/bin/env python3
"""Check measured bounds, MQTT rejection and immutable alert evidence; rollback all fixtures."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SQL = r'''
BEGIN;
SELECT pg_advisory_xact_lock(7349203);
DO $$
DECLARE observation uuid := gen_random_uuid(); later uuid := gen_random_uuid();
 bad_id uuid; good_id uuid; good jsonb; payload jsonb; malformed jsonb; bad jsonb; aid bigint; r configuration.alert_rules;
 sid text := 'range-check-'||replace(gen_random_uuid()::text,'-','');
 n int; bounds record;
BEGIN
 INSERT INTO configuration.alert_rules(id,sensor_id,enabled,level_warning,level_critical,
 rise_warning,rise_critical,rise_period_seconds,rise_window_seconds,rise_min_seconds,
 level_hysteresis,rise_hysteresis,hold_seconds,stale_seconds)
 VALUES(sid,sid,false,1,2,.1,.2,60,60,1,0,0,0,3600) RETURNING * INTO r;
 payload := jsonb_build_object('id',observation,'sensor_id',sid,'sensor_type','water-level',
  'timestamp',to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'value',1.5,'unit','m','metadata',jsonb_build_object('minimum',1.1,'maximum',1.9));
 INSERT INTO sensor.mqtt_ingest(time,value) VALUES(now(),payload::text);
 SELECT minimum,maximum,value INTO bounds FROM sensor.dashboard_values WHERE observation_id=observation;
 IF bounds.minimum IS DISTINCT FROM 1.1::float8 OR bounds.maximum IS DISTINCT FROM 1.9::float8 THEN
  RAISE EXCEPTION 'MQTT range roundtrip failed'; END IF;
 PERFORM alerting.transition(r,'level','warning',now(),now(),
  jsonb_build_object('observation_id',observation,'value',1.5,'unit','m'));
 SELECT id INTO STRICT aid FROM alerting.alerts WHERE rule_id=sid;
 payload := jsonb_set(payload,'{id}',to_jsonb(later));
 payload := jsonb_set(payload,'{value}','1.7');
 INSERT INTO sensor.mqtt_ingest(time,value) VALUES(now(),payload::text);
 -- A later reading/escalation must not change the original opening snapshot.
 PERFORM alerting.transition(r,'level','critical',now(),now(),
  jsonb_build_object('observation_id',later,'value',1.7,'unit','m'));
 DELETE FROM sensor.measurements WHERE id IN (observation,later);
 SELECT count(*) INTO n FROM alerting.events WHERE alert_id=aid AND event_type='opened'
 AND details->>'value'='1.5' AND details->>'minimum'='1.1' AND details->>'maximum'='1.9';
 IF n<>1 THEN RAISE EXCEPTION 'Opening evidence changed or lost bounds'; END IF;
 FOR malformed IN SELECT value FROM jsonb_array_elements('[
 {"minimum":1}, {"maximum":2}, {"minimum":null,"maximum":2},
 {"minimum":"1","maximum":2}, {"minimum":2,"maximum":1},
 {"minimum":1.8,"maximum":2}, {"minimum":1,"maximum":1.6},
 {"minimum":1,"maximum":1e400}]'::jsonb) LOOP
  bad_id := gen_random_uuid();
  bad := jsonb_set(jsonb_set(payload,'{id}',to_jsonb(bad_id)),'{metadata}',malformed);
  good_id := gen_random_uuid();
  good := jsonb_set(payload,'{id}',to_jsonb(good_id));
  -- Mix a valid member with an invalid member: the entire MQTT batch must roll back.
  INSERT INTO sensor.mqtt_ingest(time,value) VALUES(now(),jsonb_build_array(good,bad)::text);
  IF EXISTS (SELECT FROM sensor.events WHERE id IN (bad_id,good_id)) THEN
   RAISE EXCEPTION 'Invalid range persisted'; END IF;
  IF NOT EXISTS (SELECT FROM sensor.rejected_messages rejected WHERE rejected.payload=jsonb_build_array(good,bad)::text) THEN
   RAISE EXCEPTION 'Invalid range was not quarantined'; END IF;
 END LOOP;
 payload := jsonb_set(payload,'{id}',to_jsonb(gen_random_uuid())) - 'metadata';
 INSERT INTO sensor.mqtt_ingest(time,value) VALUES(now(),payload::text);
 IF NOT EXISTS (SELECT FROM sensor.dashboard_values WHERE observation_id=(payload->>'id')::uuid
 AND minimum IS NULL AND maximum IS NULL) THEN RAISE EXCEPTION 'Optional bounds failed'; END IF;
END $$;
ROLLBACK;
'''
result = subprocess.run(['docker','compose','-f','deploy/compose.yaml','exec','-T','timescaledb',
 'psql','-X','-q','-U','postgres','-d','sensors','-v','ON_ERROR_STOP=1'],
 input=SQL,text=True,capture_output=True,cwd=ROOT)
if result.returncode:
 raise SystemExit(result.stderr)
print('PASS: MQTT bounds, invalid range quarantine, optional bounds and opening evidence survive later readings and retention; fixtures rolled back.')
