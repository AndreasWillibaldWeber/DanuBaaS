#!/usr/bin/env python3
"""Build the provisioned monitoring dashboard from shared, reviewable SQL."""
import argparse
import json
from pathlib import Path

OUTPUT = Path(__file__).parent / 'dashboards/sensors.json'
SOURCE = {'type': 'postgres', 'uid': 'sensor-postgres'}


def station(column):
    names = {'demo-stable': 'WL-001', 'demo-fluctuating': 'WL-002',
             'demo-elevated': 'WL-003', 'demo-rapid-rise': 'WL-004', 'quickstart': 'WL-001'}
    matches = ' '.join(f"WHEN {column}='{key}' THEN '{value}'" for key, value in names.items())
    return f"""CASE {matches}
WHEN {column} ~ '^demo-alert-[0-9a-f]{{12}}-(level|rise)-(warning|critical)$'
THEN 'WL-' || split_part({column},'-',3) || '-' ||
CASE split_part({column},'-',4) || '-' || split_part({column},'-',5)
WHEN 'level-warning' THEN '005' WHEN 'level-critical' THEN '006'
WHEN 'rise-warning' THEN '007' ELSE '008' END ELSE {column} END"""


def selected(column):
    return column + ' IN (${sensor:sqlstring})'


# Read the same baseline window used by the evaluator. Normalize rates and limits
# to m/min so maximum selection is meaningful across differently configured rules.
SNAPSHOT = f"""WITH snapshot AS (
 SELECT r.*, {station('r.sensor_id')} AS station,
        latest.time AS observed_at, latest.value AS level, latest.location_id,
        baseline.time AS baseline_at,
        ((latest.value::numeric-baseline.value::numeric) /
          NULLIF(extract(epoch FROM latest.time-baseline.time),0) * 60) AS rise,
        CASE WHEN NOT r.enabled THEN 'Monitoring disabled'
             WHEN latest.time IS NULL THEN 'No reading'
             WHEN latest.time < r.updated_at THEN 'Awaiting new reading'
             WHEN latest.time < now()-make_interval(secs=>r.stale_seconds) THEN 'Stale reading'
             ELSE 'Fresh' END AS reading_status
 FROM configuration.alert_rules r
 LEFT JOIN LATERAL (
   SELECT time,value,location_id FROM sensor.dashboard_values
   WHERE sensor_id=r.sensor_id AND sensor_type='water-level' AND unit='m'
     AND time<=now() AND value>'-Infinity'::float8 AND value<'Infinity'::float8
   ORDER BY time DESC, observation_id LIMIT 1
 ) latest ON true
 LEFT JOIN LATERAL (
   SELECT time,value FROM sensor.dashboard_values
   WHERE sensor_id=r.sensor_id AND sensor_type='water-level' AND unit='m'
     AND value>'-Infinity'::float8 AND value<'Infinity'::float8
     AND time>=latest.time-make_interval(secs=>r.rise_window_seconds)
     AND time<=latest.time-make_interval(secs=>r.rise_min_seconds)
   ORDER BY time, observation_id LIMIT 1
 ) baseline ON true
 WHERE {selected('r.sensor_id')}
)"""


def card_sql(kind, severity):
    metric = 'level' if kind == 'level' else 'rise'
    limit = f'level_{severity}::numeric' if kind == 'level' else f'rise_{severity}::numeric*60/rise_period_seconds'
    return SNAPSHOT + f""", candidates AS (
 SELECT *, CASE WHEN reading_status='Fresh' THEN {metric}::numeric END AS current_value,
        {limit} AS limit_value,
        CASE WHEN reading_status='Fresh' AND '{kind}'='rise' AND baseline_at IS NULL
             THEN 'No rise baseline' ELSE reading_status END AS status
 FROM snapshot WHERE enabled
), winner AS (
 SELECT * FROM candidates
 ORDER BY current_value DESC NULLS LAST, sensor_id LIMIT 1
)
SELECT CASE WHEN side.position=0 THEN
         coalesce('Current' || chr(10) || winner.station ||
           CASE WHEN winner.status='Fresh' THEN '' ELSE chr(10) || winner.status END,
           'Current' || chr(10) || 'No enabled rule')
       ELSE coalesce('Limit' || chr(10) || winner.station, 'Limit' || chr(10) || 'Not configured') END AS name,
       CASE WHEN side.position=0 THEN winner.current_value ELSE winner.limit_value END AS value,
       winner.sensor_id, winner.location_id
FROM (VALUES (0),(1)) side(position) LEFT JOIN winner ON true ORDER BY side.position"""


def panel(identity, title, kind, sql, x, y, w, h, **extra):
    return dict(id=identity, title=title, type=kind, gridPos=dict(x=x, y=y, w=w, h=h),
                datasource=SOURCE, targets=[dict(refId='A', format='table', rawSql=sql, editorMode='code')],
                fieldConfig={'defaults': {}, 'overrides': []}, options={}, **extra)


def dashboard():
    panels = []
    for index, (kind, severity) in enumerate([('level','warning'),('level','critical'),('rise','warning'),('rise','critical')]):
        title = f'{severity.title()} · ' + ('Water level' if kind=='level' else 'Rise rate')
        unit = 'm' if kind == 'level' else 'm/min'
        # Keep numeric comparisons in card_sql; format only the final presentation.
        raw = card_sql(kind, severity)
        display_sql = f"""WITH paired AS ({raw}), card_values AS (
 SELECT max(value) FILTER (WHERE name LIKE 'Current%') AS current_value,
        max(value) FILTER (WHERE name LIKE 'Limit%') AS limit_value,
        max(sensor_id) AS sensor_id,
        max(split_part(name,chr(10),3)) AS reason
 FROM paired
)
SELECT coalesce({station('sensor_id')},'—') AS station,
 CASE WHEN sensor_id IS NULL THEN '—'
      WHEN current_value IS NULL THEN '-.-- {unit}'
      ELSE round(current_value,{2 if kind=='level' else 3})::text || ' {unit}' END AS current_display,
 CASE WHEN limit_value IS NULL THEN '—'
      ELSE round(limit_value,{2 if kind=='level' else 3})::text || ' {unit}' END AS limit_display,
 CASE WHEN sensor_id IS NULL THEN 'Not configured' ELSE coalesce(nullif(reason,''),'Fresh reading') END AS status
FROM card_values"""
        card = panel(10+index, title, 'canvas', display_sql, index*6,0,6,5,
                     description='Current value at left; matching limit at right. Station labels appear on the second line. All selects the highest fresh value. Missing data: -.-- with unit; no enabled rule: —. Rise rates are normalized to m/min.')
        elements = []
        def text_element(name, text, left, top, size, field=False, width=180):
            elements.append({'name':name,'type':'metric-value',
                'config':{'align':'center','valign':'middle','size':size,
                    'color':{'fixed':'#FFFFFF'},
                    'text':{'mode':'field' if field else 'fixed',
                            'field':text if field else '', 'fixed':'' if field else text}},
                'placement':{'left':left,'top':top,'width':width,'height':32},
                'constraint':{'horizontal':'left','vertical':'top'},
                'background':{'color':{'fixed':'transparent'}}})
        for label, left in [('Current', 0), ('Limit', 190)]:
            text_element(label+' heading', label, left, 0, 16)
            text_element(label+' station', 'station', left, 30, 14, True)
            text_element(label+' value', label.lower()+'_display', left, 65, 27, True)
        text_element('Reading status','status',0,105,12,True,370)
        card['options'] = {'inlineEditing':False,'panZoom':True,'zoomToContent':True,
            'showAdvancedTypes':False,'tooltip':{'mode':'none'},
            'root':{'name':'Monitoring card','type':'frame','elements':elements,
                'placement':{'width':370,'height':137},
                'background':{'color':{'fixed':'#A65300' if severity=='warning' else '#A82020'}}}}
        panels.append(card)
    chart = panel(1,'Water levels','timeseries',f"""SELECT time,value,{station('sensor_id')} AS metric
FROM sensor.dashboard_values WHERE $__timeFilter(time) AND sensor_type='water-level' AND unit='m'
AND {selected('sensor_id')} ORDER BY time""",0,5,24,10)
    chart['targets'][0]['format']='time_series'
    chart['fieldConfig']['defaults']={'unit':'suffix: m'}
    panels.append(chart)
    observations=panel(2,'Recent observations','table',f"""SELECT time,{station('sensor_id')} AS station,
sensor_id,sensor_type,value,unit,longitude,latitude,location_id,metadata
FROM sensor.dashboard_values WHERE $__timeFilter(time) AND {selected('sensor_id')}
ORDER BY time DESC LIMIT 100""",0,15,24,9)
    alerts=panel(3,'Active alerts','table',f"""SELECT a.id,{station('r.sensor_id')} AS station,
location.location_id,r.sensor_id,a.kind,a.severity,a.opened_at,a.acknowledged_at,a.acknowledged_by
FROM alerting.alerts a JOIN configuration.alert_rules r ON r.id=a.rule_id
LEFT JOIN LATERAL (SELECT location_id FROM sensor.dashboard_values WHERE sensor_id=r.sensor_id
ORDER BY time DESC LIMIT 1) location ON true
WHERE a.resolved_at IS NULL AND {selected('r.sensor_id')} ORDER BY a.opened_at DESC LIMIT 100""",0,24,24,9,
                 description='Current unresolved alerts for the selected sensors; independent of the historical time range.')
    for item in (observations,alerts):
        item['fieldConfig']['overrides']=[{'matcher':{'id':'byName','options':'sensor_id'},
                                           'properties':[{'id':'custom.hidden','value':True}]}]
        panels.append(item)
    health=panel(4,'Monitoring status','table',SNAPSHOT+"""
SELECT snapshot.station,snapshot.location_id,snapshot.enabled,snapshot.reading_status,
snapshot.observed_at,snapshot.baseline_at,e.last_success_at,e.schedule_seconds,
e.last_success_at>now()-make_interval(secs=>e.schedule_seconds*3) AS evaluator_healthy
FROM snapshot CROSS JOIN alerting.evaluator_status e ORDER BY snapshot.sensor_id""",0,33,24,8,
                 description='Selected sensors with configured rules. The evaluator heartbeat is global; freshness and baseline availability are sensor-specific.')
    panels.append(health)
    variable_sql=f"""SELECT {station('sensor_id')} AS __text,sensor_id AS __value FROM (
SELECT DISTINCT sensor_id FROM sensor.dashboard_values WHERE sensor_type='water-level' AND unit='m'
UNION SELECT sensor_id FROM configuration.alert_rules) sensors ORDER BY __text"""
    return {'uid':'sensors','title':'Water-level monitoring','schemaVersion':39,'version':4,
            'refresh':'10s','time':{'from':'now-24h','to':'now'},
            'templating':{'list':[{'name':'sensor','label':'Monitored sensor','type':'query',
                'datasource':SOURCE,'query':variable_sql,'definition':variable_sql,'refresh':1,
                'multi':False,'includeAll':True,'sort':1,'options':[],
                'current':{'selected':True,'text':'All','value':'$__all'}}]},'panels':panels}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    rendered=json.dumps(dashboard(),indent=2)+'\n'
    if args.check:
        if OUTPUT.read_text()!=rendered:
            raise SystemExit('Dashboard JSON is out of date; run python3 deploy/grafana/build_dashboard.py')
    else:
        OUTPUT.write_text(rendered)
