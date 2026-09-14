#!/usr/bin/env python3
"""Verify development reset protection in rolled-back PostgreSQL transactions."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
COMMAND = ['docker', 'compose', '-f', 'deploy/compose.yaml', 'exec', '-T', 'timescaledb',
           'psql', '-X', '-qAt', '-U', 'postgres', '-d', 'sensors', '-v', 'ON_ERROR_STOP=1']
reset = (ROOT / 'deploy/scripts/reset_test_data.sql').read_text()
# Removing BEGIN lets fixtures and the reset share one transaction; no test commits.
reset = reset.replace('BEGIN;', '', 1).replace('COMMIT;', '')

def run(sql):
    return subprocess.run(COMMAND, input=sql, text=True, capture_output=True, cwd=ROOT)

# A reserved sensor with a non-synthetic observation must abort before any deletion.
conflict = """BEGIN;
INSERT INTO sensor.events(id,observed_at,payload) VALUES
 ('fd1f9b80-606e-4d1e-9d18-d4299e385122',now(),
 '{"sensor_id":"WL-002","gateway_id":"operator","metadata":{}}');
"""
result = run(conflict + reset + 'ROLLBACK;')
assert result.returncode != 0 and 'Reserved test sensor contains non-fixture' in result.stderr, result.stderr

# A normal reset removes fixture data but preserves quickstart and unrelated data.
result = run("""BEGIN;
CREATE TEMP TABLE retained AS SELECT * FROM sensor.events
 WHERE payload->>'sensor_id' NOT IN ('WL-002','WL-003','WL-004','WL-005')
 AND payload->>'sensor_id' !~ '^demo-';
""" + reset + """
DO $$ BEGIN
 IF EXISTS (SELECT * FROM retained EXCEPT SELECT * FROM sensor.events)
 THEN RAISE EXCEPTION 'Reset removed unrelated observations'; END IF;
 IF EXISTS (SELECT FROM sensor.events WHERE payload->>'sensor_id' IN ('WL-002','WL-003','WL-004','WL-005'))
 THEN RAISE EXCEPTION 'Reset retained fixture observations'; END IF;
 IF EXISTS (SELECT FROM configuration.alert_rules WHERE id LIKE 'dev-test-%')
 THEN RAISE EXCEPTION 'Reset retained fixture rules'; END IF;
END $$;
ROLLBACK;
""")
assert result.returncode == 0, result.stderr
print('PASS: reset refuses non-fixture data, removes reserved fixtures, preserves unrelated data; all checks rolled back.')
