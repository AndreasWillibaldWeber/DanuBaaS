#!/usr/bin/env python3
"""Check dashboard SQL against PostgreSQL temporary fixtures and the live reader role.

Requires the development Compose database. Fixtures are connection-local and rolled back.
"""
import json
import math
from pathlib import Path
import subprocess

from build_dashboard import dashboard, card_sql

ROOT = Path(__file__).resolve().parents[2]
FIXED = "'2026-09-15T12:00:00Z'::timestamptz"


def query(sql, sensors, fixtures=False):
    values = ','.join("'" + item.replace("'", "''") + "'" for item in sensors) or "''"
    sql = sql.replace('${sensor:sqlstring}', values).replace('$__timeFilter(time)', 'true')
    if fixtures:
        sql = sql.replace('configuration.alert_rules', 'fixture_rules').replace('sensor.dashboard_values', 'fixture_values')
        sql = sql.replace('now()', FIXED)
    return sql


def verify():
    setup = """BEGIN;
CREATE TEMP TABLE fixture_rules (LIKE configuration.alert_rules INCLUDING DEFAULTS);
CREATE TEMP TABLE fixture_values AS SELECT * FROM sensor.dashboard_values WITH NO DATA;
INSERT INTO fixture_rules(id,sensor_id,enabled,level_warning,level_critical,rise_warning,rise_critical,
 rise_period_seconds,rise_window_seconds,rise_min_seconds,level_hysteresis,rise_hysteresis,
 hold_seconds,stale_seconds,updated_at) VALUES
 ('A','A',true,3,5,1,2,300,120,10,0,0,0,120,'2026-09-15T10:00Z'),
 ('B','B',true,7,9,0.1,0.3,60,120,10,0,0,0,120,'2026-09-15T10:00Z'),
 ('C','C',true,100,200,1,2,60,120,10,0,0,0,120,'2026-09-15T10:00Z'),
 ('D','D',false,100,200,1,2,60,120,10,0,0,0,120,'2026-09-15T10:00Z'),
 ('E','E',true,2,3,0.1,0.2,60,120,10,0,0,0,120,'2026-09-15T10:00Z');
INSERT INTO fixture_values(time,sensor_id,sensor_type,unit,value,observation_id) VALUES
 ('2026-09-15T11:59Z','A','water-level','m',3,'00000000-0000-0000-0000-000000000001'),
 ('2026-09-15T11:59:50Z','A','water-level','m',4,'00000000-0000-0000-0000-000000000002'),
 ('2026-09-15T12:01Z','A','water-level','m',999,'00000000-0000-0000-0000-000000000003'),
 ('2026-09-15T11:59:59Z','A','water-level','mm',999,'00000000-0000-0000-0000-000000000004'),
 ('2026-09-15T11:59Z','B','water-level','m',7.9,'00000000-0000-0000-0000-000000000005'),
 ('2026-09-15T11:59:50Z','B','water-level','m',8,'00000000-0000-0000-0000-000000000006'),
 ('2026-09-15T11:00Z','C','water-level','m',99,'00000000-0000-0000-0000-000000000007'),
 ('2026-09-15T11:59:50Z','D','water-level','m',999,'00000000-0000-0000-0000-000000000008'),
 ('2026-09-15T11:59:50Z','E','water-level','m',1.5,'00000000-0000-0000-0000-000000000009');
"""
    checks = []
    def check(kind, severity, sensors, current, limit, sensor, status=None):
        sql = query(card_sql(kind,severity), sensors, True)
        checks.append((sql, current, limit, sensor, status))
    all_sensors=['A','B','C','D','E']
    check('level','warning',all_sensors,8,7,'B')
    check('level','critical',all_sensors,8,9,'B')
    check('rise','warning',all_sensors,1.2,.2,'A')
    check('rise','critical',all_sensors,1.2,.4,'A')
    check('level','warning',['A'],4,3,'A')
    check('rise','warning',['B'],.12,.1,'B')
    check('level','warning',['C'],None,100,'C','Stale reading')
    check('rise','warning',['E'],None,.1,'E','No rise baseline')
    check('level','warning',['D'],None,None,None,'No enabled rule')
    check('level','warning',["unknown's sensor"],None,None,None,'No enabled rule')
    check('level','warning',[],None,None,None,'No enabled rule')
    sql=setup
    for item in checks:
        sql+='SELECT jsonb_agg(to_jsonb(result)) FROM ('+item[0]+') result;\n'
    # Timestamp ties use observation identity, not whichever value happens to be largest.
    sql+="INSERT INTO fixture_values(time,sensor_id,sensor_type,unit,value,observation_id) VALUES ('2026-09-15T11:59Z','A','water-level','m',100,'00000000-0000-0000-0000-000000000099');\n"
    extra=query(card_sql('rise','warning'),['A'],True)
    sql+='SELECT jsonb_agg(to_jsonb(result)) FROM ('+extra+') result;\n'
    checks.append((extra,1.2,.2,'A',None))
    presentations = [
        (0, ['B'], '8.00 m', '7.00 m', 'B'),
        (0, ['C'], '-.-- m', '100.00 m', 'C'),
        (2, ['E'], '-.-- m/min', '0.100 m/min', 'E'),
        (0, ['missing'], '—', '—', '—'),
        (2, ['D'], '—', '—', '—'),
    ]
    for index, sensors, *_ in presentations:
        rendered = query(dashboard()['panels'][index]['targets'][0]['rawSql'], sensors, True)
        sql += 'SELECT row_to_json(result) FROM ('+rendered+') result;\n'
    sql+='ROLLBACK;\nSET ROLE grafana_reader;\n'

    # Exercise every real panel and the dropdown with existing production grants.
    for panel in dashboard()['panels']:
        sql+='SELECT count(*) FROM ('+query(panel['targets'][0]['rawSql'],['quickstart'])+') result;\n'
    sql+='SELECT count(*) FROM ('+dashboard()['templating']['list'][0]['query']+') result;\n'
    result=subprocess.run(['docker','compose','-f','deploy/compose.yaml','exec','-T','timescaledb',
                           'psql','-X','-qAt','-U','postgres','-d','sensors','-v','ON_ERROR_STOP=1'],
                          input=sql,text=True,capture_output=True,cwd=ROOT,check=True)
    rows=[json.loads(line) for line in result.stdout.splitlines() if line.startswith('[')]
    assert len(rows)==len(checks), 'missing SQL results'
    for actual,(_,current,limit,sensor,status) in zip(rows,checks):
        assert len(actual)==2,actual
        for field,expected in zip(actual,[current,limit]):
            assert field['sensor_id']==sensor,(actual,sensor)
            assert field['value'] is None if expected is None else math.isclose(field['value'],expected,rel_tol=1e-9),(actual,expected)
        if status:
            assert status in actual[0]['name'],actual
    rendered = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    assert len(rendered) == len(presentations)
    for actual, (_, _, current, limit, station) in zip(rendered, presentations):
        assert (actual['current_display'], actual['limit_display'], actual['station']) == (current, limit, station), actual
    print(f'PASS: {len(checks)} monitoring cases and {len(presentations)} display cases; all panels and dropdown execute with grafana_reader privileges.')


if __name__=='__main__':
    verify()
