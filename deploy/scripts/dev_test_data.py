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
PROFILES = ('stable', 'fluctuating', 'elevated', 'rapid-rise')
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


def dataset(anchor):
    """Version 2: 145 points per sensor, ten-minute spacing over 24 hours."""
    stamp = anchor.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    rows = []
    for index, profile in enumerate(PROFILES):
        for step in range(145):
            if profile == 'stable':
                level = 1.2 + 0.025 * math.sin(step / 6)
            elif profile == 'fluctuating':
                level = 1.65 + 0.4 * math.sin(step / 12)
            elif profile == 'elevated':
                level = 2.1 + 1.25 * step / 144
            else:
                level = 0.9 + max(0, step - 141) * 0.4
            observation = dict(
                id=str(uuid.uuid5(NAMESPACE, f'v2/{stamp}/{profile}/{step}')),
                sensor_id='demo-' + profile, gateway_id='demo-test-data',
                sensor_type='water-level', unit='m', value=round(level, 4),
                timestamp=(anchor - timedelta(minutes=(144 - step) * 10)).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                metadata={'synthetic': True, 'dataset': 'dev-test-data-v2',
                          'profile': profile, 'anchor': stamp,
                          'ingestion_route': 'api' if index % 2 == 0 else 'node-red'})
            observation['location_id'] = 9000 + index
            if index % 2 == 0:
                observation['lon_lat'] = [12.96 + index * 0.001, 48.83]
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
    identity = f'demo-alert-{run}-{kind}-{severity}'
    return dict(id=identity, sensor_id=identity, enabled=True,
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


def load_alert_demo(client, admin, backend, timeout):
    # A unique sensor/rule set avoids changing operator rules or using old rise baselines.
    run = uuid.uuid4().hex[:12]
    print(f'Creating isolated alert demo run {run}.', flush=True)
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
    for index, rule in enumerate(rules):
        kind, severity = rule['id'].split('-')[-2:]
        row = dict(id=str(uuid.uuid4()), sensor_id=rule['sensor_id'], gateway_id='demo-test-data',
                   sensor_type='water-level', unit='m', location_id=9004 + index,
                   value=(2.4 if severity == 'warning' else 3.4) if kind == 'level' else 0.8,
                   timestamp=baseline_time.isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                   metadata={'synthetic': True, 'dataset': 'dev-test-alerts-v1', 'test_run': run,
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
        if '-rise-' not in row['sensor_id']:
            continue
        severity = row['sensor_id'].split('-')[-1]
        rate = 0.15 if severity == 'warning' else 0.3
        updated = {**row, 'id': str(uuid.uuid4()), 'value': round(0.8 + rate * elapsed / 60, 8),
                   'timestamp': final_time.isoformat(timespec='microseconds').replace('+00:00', 'Z')}
        final.append(updated)
        expected[(row['sensor_id'], 'rise', severity)] = updated['id']
    load(client, final, backend)
    print('Waiting for PostgreSQL to produce level/rise warning and critical alerts...', flush=True)
    alerts = verify_alerts(client, expected, timeout)
    print('Verified four active demo alerts and their observation evidence.', flush=True)
    return {'run': run, 'rules': rules, 'observations': first + final, 'alerts': alerts}


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
        print(f'Synthetic dataset v2: {len(rows)} observations; anchor {anchor.isoformat()}', flush=True)
        print(f'Replay with --at {anchor.isoformat()}; payload saved in {target.relative_to(ROOT)}', flush=True)
        client = Curl(key, ca)
        backend = config.get('INGESTION_BACKEND', 'api')
        load(client, rows, backend)
        report = load_alert_demo(client, Curl(admin_key, ca), backend, args.alert_timeout)
        (directory / ('alerts-' + report['run'] + '.json')).write_text(json.dumps(report, indent=2) + '\n')
        print('Dataset verified. Open Grafana with a time range including the dataset anchor.')
        return 0
    except (CheckFailed, ConfigurationError, OSError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, (CheckFailed, ConfigurationError)) else 'local file or process operation failed'
        print('FAIL: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
