#!/usr/bin/env python3
"""Load reproducible synthetic observations through the local development endpoints."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from e2e import CheckFailed, Curl, read_event, require, verify_record
from environment import read_env, ConfigurationError

ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = uuid.UUID('e53bf019-43e7-4a70-a539-e6529fb0699c')
PROFILES = ('level-warning', 'level-critical', 'rise-warning', 'rise-critical')
SENSORS = dict(zip(PROFILES, ('WL-002', 'WL-003', 'WL-004', 'WL-005')))
SCENARIOS = {('level', 'warning'): 'WL-002', ('level', 'critical'): 'WL-003',
             ('rise', 'warning'): 'WL-004', ('rise', 'critical'): 'WL-005'}
API = 'https://api.localhost'
NODE_RED = 'https://flows.localhost'


def anchor_time(value=None, now=None):
    now = now or datetime.now(timezone.utc)
    if value is None:
        return now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None, '--at must include a timezone')
        result = result.astimezone(timezone.utc)
        require(result <= now, '--at cannot be in the future')
        require(result >= datetime(1, 1, 2, tzinfo=timezone.utc), '--at must allow 24 hours of history')
        return result
    except ValueError:
        raise CheckFailed('--at must be an ISO 8601 timestamp with a timezone') from None


def historical_level(profile, step):
    """Smooth histories ending at the baseline of each live alert scenario."""
    progress = step / 144
    if profile == 'level-warning':
        return round(1.4 + progress, 4)
    if profile == 'level-critical':
        return round(2.1 + 1.3 * progress, 4)
    start = 0.7 if profile == 'rise-warning' else 0.6
    return round(start + (0.8 - start) * progress
                 + 0.01 * math.sin(step / 6) * (1 - progress), 4)


def station_attributes(profile):
    index = PROFILES.index(profile)
    attributes = {'location_id': 9000 + index}
    if index % 2 == 0:
        attributes['lon_lat'] = [12.96 + index * 0.001, 48.83]
    return attributes


def dataset(anchor):
    """Version 4: 145 points per sensor, ten-minute spacing over 24 hours."""
    stamp = anchor.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    rows = []
    for index, profile in enumerate(PROFILES):
        for step in range(145):
            level = historical_level(profile, step)
            observation = dict(
                id=str(uuid.uuid5(NAMESPACE, f'v4/{stamp}/{profile}/{step}')),
                sensor_id=SENSORS[profile], gateway_id='demo-test-data',
                sensor_type='water-level', unit='m', value=round(level, 4),
                timestamp=(anchor - timedelta(minutes=(144 - step) * 10)).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                metadata={'synthetic': True, 'dataset': 'dev-test-data-v4',
                          'profile': profile, 'anchor': stamp,
                          'ingestion_route': 'api' if index % 2 == 0 else 'node-red'})
            observation.update(station_attributes(profile))
            rows.append(observation)
    return rows


def load(client, rows, backend, timeout=30):
    """Exercise single and batch writes, then verify persistence through both routes."""
    require(backend in ('api', 'mqtt'), 'invalid Node-RED ingestion backend')
    counts = {}
    for route, endpoint in [('api', API), ('node-red', NODE_RED)]:
        selected = [row for row in rows if row['metadata']['ingestion_route'] == route]
        require(bool(selected), 'dataset must exercise both ingestion routes')
        bodies = [selected[0]] + [selected[i:i + 100] for i in range(1, len(selected), 100)]
        for body in bodies:
            submitted = body if isinstance(body, list) else [body]
            status, response = client.request(endpoint, 'POST', body)
            if route == 'node-red' and backend == 'mqtt':
                require(status == 202 and isinstance(response, dict)
                        and response.get('status') == 'accepted'
                        and response.get('ids') == [row['id'] for row in submitted],
                        'Node-RED did not return the expected MQTT acceptance')
            else:
                require(status in (200, 201), f'{route} write returned HTTP {status}')
                returned = response if isinstance(body, list) else [response]
                require(isinstance(returned, list) and len(returned) == len(submitted), 'invalid write response shape')
                for record, row in zip(returned, submitted):
                    verify_record(record, row)
            for row in submitted:
                stored = read_event(client, API, row, timeout)
                require(read_event(client, NODE_RED, row, timeout) == stored,
                        'read endpoints disagree')
        counts[route] = len(selected)
        print(f'Verified {len(selected)} observations written through {route} and read through both endpoints.', flush=True)
    return counts


def alert_rule(run, kind, severity):
    identity = f'dev-test-{kind}-{severity}'
    return dict(id=identity, sensor_id=SCENARIOS[kind, severity], enabled=True,
                level_warning=2, level_critical=3, rise_warning=0.1, rise_critical=0.2,
                rise_period_seconds=60, rise_window_seconds=60, rise_min_seconds=1,
                level_hysteresis=0.05, rise_hysteresis=0.02,
                hold_seconds=0, stale_seconds=3600, version=0)


def active_alerts(client):
    result, after = [], 0
    for _ in range(100):
        status, page = client.request(API, query={'active': 'true', 'limit': 1000, 'after': after}, resource='/api/v1/alerts')
        require(status == 200 and isinstance(page, list), 'cannot read active alerts')
        for alert in page:
            require(isinstance(alert, dict) and type(alert.get('id')) is int and alert['id'] > after,
                    'invalid alert pagination')
            after = alert['id']
            result.append(alert)
        if len(page) < 1000:
            return result
    raise CheckFailed('active alerts exceeded verification page limit')


def verify_alerts(client, expected, timeout):
    deadline = time.monotonic() + timeout
    while True:
        found = {(a.get('rule_id'), a.get('kind'), a.get('severity')): a for a in active_alerts(client)}
        if all(key in found for key in expected):
            rule_ids = {key[0] for key in expected}
            require({key for key in found if key[0] in rule_ids} == set(expected),
                    'unexpected additional alert condition on a test sensor')
            for key, observation_id in expected.items():
                alert = found[key]
                status, events = client.request(API, resource=f'/api/v1/alerts/{alert["id"]}/events', query={'limit': 1000})
                require(status == 200 and isinstance(events, list) and any(
                    isinstance(e, dict) and isinstance(e.get('details'), dict)
                    and e['details'].get('observation_id') == observation_id for e in events),
                    'alert history does not contain the submitted observation')
            return [found[key] for key in expected]
        require(time.monotonic() < deadline, 'scheduled evaluator did not produce all four expected demo alerts in time')
        time.sleep(1)


def load_alert_demo(client, admin, backend, timeout, history_end=None):
    # Historical data shares these sensors. Keep it outside the 60-second live
    # baseline window, including loads during the first minute of an hour.
    if history_end is not None:
        remaining = (history_end + timedelta(seconds=61) - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            print(f'Waiting {remaining:.0f}s for historical readings to leave the live rise window.', flush=True)
            while remaining > 0:
                time.sleep(min(remaining, 30))
                remaining = (history_end + timedelta(seconds=61) - datetime.now(timezone.utc)).total_seconds()
    # Reserved fixture rules are recreated after resetting only loader-owned data.
    run = uuid.uuid4().hex[:12]
    print(f'Creating five-station demo run {run}.', flush=True)
    rules, first, expected = [], [], {}
    for kind in ('level', 'rise'):
        for severity in ('warning', 'critical'):
            rule = alert_rule(run, kind, severity)
            status, saved = admin.request(API, 'POST', rule, resource='/api/v1/alert-rules')
            require(status == 201 and isinstance(saved, dict)
                    and all(saved.get(key) == (1 if key == 'version' else value) for key, value in rule.items()),
                    'demo alert rule creation failed')
            rules.append(saved)
    baseline_time = datetime.now(timezone.utc)
    for rule in rules:
        kind, severity = rule['id'].split('-')[-2:]
        row = dict(id=str(uuid.uuid4()), sensor_id=rule['sensor_id'], gateway_id='demo-test-data',
                   sensor_type='water-level', unit='m', **station_attributes(f'{kind}-{severity}'),
                   value=historical_level(f'{kind}-{severity}', 144),
                   timestamp=baseline_time.isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                   metadata={'synthetic': True, 'dataset': 'dev-test-alerts-v2', 'test_run': run,
                             'profile': f'{kind}-{severity}',
                             'ingestion_route': 'api' if severity == 'warning' else 'node-red'})
        first.append(row)
        if kind == 'level':
            expected[(rule['id'], kind, severity)] = row['id']
    load(client, first, backend)
    time.sleep(2)
    final_time = datetime.now(timezone.utc)
    elapsed = (final_time - baseline_time).total_seconds()
    require(1 < elapsed < 60, 'rise baseline fell outside its window; rerun on a responsive development stack')
    final = []
    for row in first:
        if row['sensor_id'] not in (SCENARIOS['rise', 'warning'], SCENARIOS['rise', 'critical']):
            continue
        severity = 'warning' if row['sensor_id'] == SCENARIOS['rise', 'warning'] else 'critical'
        rate = 0.15 if severity == 'warning' else 0.3
        updated = {**row, 'id': str(uuid.uuid4()), 'value': round(row['value'] + rate * elapsed / 60, 8),
                   'timestamp': final_time.isoformat(timespec='microseconds').replace('+00:00', 'Z')}
        final.append(updated)
        expected[(f'dev-test-rise-{severity}', 'rise', severity)] = updated['id']
    load(client, final, backend)
    print('Waiting for PostgreSQL to produce level/rise warning and critical alerts...', flush=True)
    alerts = verify_alerts(client, expected, timeout)
    print('Verified four active demo alerts and their observation evidence.', flush=True)
    return {'run': run, 'rules': rules, 'observations': first + final, 'alerts': alerts}


def reset_fixture_data():
    """Reset reserved synthetic fixtures, retaining quickstart and operator data."""
    sql = (ROOT / 'deploy/scripts/reset_test_data.sql').read_text()
    result = subprocess.run(
        ['docker', 'compose', '-f', 'deploy/compose.yaml', 'exec', '-T', 'timescaledb',
         'psql', '-X', '-q', '-U', 'postgres', '-d', 'sensors', '-v', 'ON_ERROR_STOP=1'],
        input=sql, text=True, capture_output=True, cwd=ROOT)
    require(result.returncode == 0,
            'Cannot reset reserved test fixtures; check Docker access and reserved sensor ownership')
    print('Reset loader-owned test observations and alert history; retained quickstart and operator data.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--alert-timeout', type=int, default=120, help='maximum scheduled-alert wait in seconds (1..7200)')
    parser.add_argument('--at', help='dataset end timestamp; default: current UTC hour; reuse to replay exactly')
    args = parser.parse_args(argv)
    try:
        anchor = anchor_time(args.at)
        require(1 <= args.alert_timeout <= 7200, '--alert-timeout must be 1..7200 seconds')
        config = read_env(ROOT / 'deploy/.env')
        require(config.get('API_HOST') == 'api.localhost'
                and config.get('NODE_RED_API_HOST') == 'flows.localhost',
                'test data loader requires the default local development API hosts')
        require(shutil.which('curl') is not None, 'curl is required')
        key = (ROOT / 'deploy/secrets/api_key').read_text().rstrip('\r\n')
        require(len(key) >= 32 and all(33 <= ord(char) <= 126 for char in key), 'invalid development API key')
        admin_key = (ROOT / 'deploy/secrets/alert_admin_key').read_text().rstrip('\r\n')
        require(len(admin_key) >= 32 and all(33 <= ord(char) <= 126 for char in admin_key), 'invalid alert admin key')
        ca = ROOT / 'deploy/certs/caddy-root.crt'
        require(ca.is_file(), 'development CA missing; run make dev')
        rows = dataset(anchor)
        directory = ROOT / 'deploy/test-data'
        directory.mkdir(exist_ok=True)
        target = directory / 'observations.json'
        temporary = directory / 'observations.json.tmp'
        temporary.write_text(json.dumps(rows, indent=2) + '\n')
        temporary.replace(target)
        print(f'Synthetic dataset v4: {len(rows)} observations; anchor {anchor.isoformat()}', flush=True)
        print(f'Replay with --at {anchor.isoformat()}; payload saved in {target.relative_to(ROOT)}', flush=True)
        client = Curl(key, ca)
        backend = config.get('INGESTION_BACKEND', 'api')
        reset_fixture_data()
        load(client, rows, backend)
        report = load_alert_demo(client, Curl(admin_key, ca), backend, args.alert_timeout, history_end=anchor)
        (directory / ('alerts-' + report['run'] + '.json')).write_text(json.dumps(report, indent=2) + '\n')
        print('Dataset verified. Open Grafana with a time range including the dataset anchor.')
        return 0
    except (CheckFailed, ConfigurationError, OSError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, (CheckFailed, ConfigurationError)) else 'local file or process operation failed'
        print('FAIL: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
