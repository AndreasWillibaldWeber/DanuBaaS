#!/usr/bin/env python3
"""Verify running DanuBaaS HTTP endpoints with curl and real persisted observations."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[2]
RESOURCE = '/api/v1/values'


class CheckFailed(Exception):
    """A transport or application contract failed; messages never include credentials."""


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


class Curl:
    def __init__(self, key, cacert, request_timeout=5):
        self.key = key
        self.cacert = cacert
        self.request_timeout = request_timeout

    def request(self, base, method='GET', body=None, query=None, authenticated=True):
        url = base.rstrip('/') + RESOURCE
        if query:
            url += '?' + urlencode(query)
        # Ignore ~/.curlrc, retain TLS verification, and never follow redirects with a key.
        command = ['curl', '--disable', '--silent', '--show-error', '--max-time',
                   str(self.request_timeout), '--request', method, '--header', '@-',
                   '--write-out', '\n%{http_code}', '--url', url]
        if self.cacert:
            command += ['--cacert', str(self.cacert)]
        headers = 'Content-Type: application/json\n'
        if authenticated:
            headers += 'X-API-Key: ' + self.key + '\n'
        with tempfile.TemporaryDirectory(prefix='danubaas-e2e-') as directory:
            if body is not None:
                payload = Path(directory) / 'request.json'
                payload.write_text(json.dumps(body), encoding='utf-8')
                command += ['--data-binary', '@' + str(payload)]
            # The key travels over stdin, never through process arguments or diagnostics.
            result = subprocess.run(command, input=headers, text=True, capture_output=True,
                                    timeout=self.request_timeout + 2)
        require(result.returncode == 0,
                f'{method} {url}: curl failed (exit {result.returncode}); check connectivity and TLS trust')
        try:
            raw, status = result.stdout.rsplit('\n', 1)
            status = int(status)
        except ValueError:
            raise CheckFailed('curl returned an invalid HTTP status') from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise CheckFailed(f'{method} {url}: HTTP {status} did not return JSON') from None
        return status, data


def canonical(value):
    result = {key: item for key, item in value.items()
              if key not in ('lon_lat', 'location_id') or item is not None}
    # Go emits the same UTC instant without insignificant fractional-second zeros.
    timestamp = datetime.fromisoformat(result['timestamp'].replace('Z', '+00:00'))
    normalized = timestamp.astimezone(timezone.utc).isoformat(timespec='microseconds')
    result['timestamp'] = normalized.removesuffix('+00:00').rstrip('0').rstrip('.') + 'Z'
    return result


def verify_record(record, value):
    require(isinstance(record, dict), 'read response must be an observation object')
    observation = {key: item for key, item in record.items() if key not in ('sequence', 'received_at')}
    require(observation == canonical(value), f'observation content changed for {value["id"]}')
    require(type(record.get('sequence')) is int and record['sequence'] > 0, 'invalid sequence')
    try:
        received = datetime.fromisoformat(record['received_at'].replace('Z', '+00:00'))
        require(received.tzinfo is not None, 'received_at must include a timezone')
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CheckFailed('invalid received_at') from None


def read_event(client, endpoint, value, timeout):
    deadline = time.monotonic() + timeout
    while True:
        status, record = client.request(endpoint, query={'id': value['id']})
        if status == 200:
            verify_record(record, value)
            return record
        require(status == 404, f'read returned HTTP {status}; expected 200 or temporary 404')
        require(time.monotonic() < deadline, f'observation {value["id"]} not visible within {timeout}s')
        time.sleep(0.2)


def verify_list(client, endpoint, records, timeout):
    expected = {record['id']: record for record in records}
    cursor = min(record['sequence'] for record in records) - 1
    found = set()
    deadline = time.monotonic() + timeout
    for _ in range(1000):
        status, page = client.request(endpoint, query={'after': cursor, 'limit': 2})
        require(status == 200 and isinstance(page, list), 'list request failed')
        require(0 < len(page) <= 2, 'list ended before all test observations were returned')
        for record in page:
            require(isinstance(record, dict) and type(record.get('sequence')) is int,
                    'list contains an invalid record')
            require(record['sequence'] > cursor, 'list cursor did not advance')
            cursor = record['sequence']
            identity = record.get('id')
            if identity in expected:
                require(identity not in found and record == expected[identity], 'list duplicated or changed an observation')
                found.add(identity)
        if found == set(expected):
            return
        require(time.monotonic() < deadline, 'list verification timed out')
    raise CheckFailed('list exceeded 1000 pages')


def run_checks(client, api, node_red, timeout=30, node_red_backend=None):
    run_id = uuid.uuid4().hex[:12]

    def value(**fields):
        return dict(id=str(uuid.uuid4()), sensor_id='e2e-' + run_id, sensor_type='water-level',
                    timestamp=datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                    value=0, unit='m', metadata={'test_run': run_id}, **fields)

    print(f'Test run {run_id}: checking Go API and Node-RED', flush=True)
    for endpoint in (api, node_red):
        for method, body in [('GET', None), ('POST', value())]:
            status, _ = client.request(endpoint, method, body, authenticated=False)
            require(status == 401, f'{method} without API key returned HTTP {status}')

    records = []
    for label, endpoint in [('Go API', api), ('Node-RED', node_red)]:
        observations = [value(lon_lat=[16.3738, 48.2082], location_id=7),
                        value(lon_lat=[-180, -90]), value(location_id=7),
                        value(lon_lat=None, location_id=None), value(),
                        value(lon_lat=[0, 0], location_id=0)]
        for body in (observations[0], observations[1:]):
            status, response = client.request(endpoint, 'POST', body)
            allowed = (201,) if endpoint == api or node_red_backend == 'api' else ((202,) if node_red_backend == 'mqtt' else (201, 202))
            require(status in allowed,
                    f'{label} create returned HTTP {status}')
            if status == 202:
                submitted = body if isinstance(body, list) else [body]
                require(isinstance(response, dict) and response.get('status') == 'accepted'
                        and response.get('ids') == [item['id'] for item in submitted], 'invalid MQTT acceptance response')
            else:
                submitted = body if isinstance(body, list) else [body]
                returned = response if isinstance(body, list) else [response]
                require(isinstance(returned, list) and len(returned) == len(submitted), 'invalid POST response shape')
                for record, item in zip(returned, submitted):
                    verify_record(record, item)
        for observation in observations:
            stored = read_event(client, api, observation, timeout)
            require(read_event(client, node_red, observation, timeout) == stored, 'read endpoints disagree')
            records.append(stored)
        # Replay through the other writer, exchanging omitted and explicit null locations.
        other = node_red if endpoint == api else api
        replay = [{**item, **{key: None for key in ('lon_lat', 'location_id') if key not in item}}
                  for item in observations]
        status, _ = client.request(other, 'POST', replay)
        allowed = (200,) if other == api or node_red_backend == 'api' else ((202,) if node_red_backend == 'mqtt' else (200, 202))
        require(status in allowed, 'cross-route replay failed')
        for observation, original in zip(observations, records[-len(observations):]):
            require(read_event(client, api, observation, timeout) == original, 'retry changed stored identity')
        fresh = value()
        status, _ = client.request(endpoint, 'POST', [fresh, value(lon_lat=[1, None])])
        require(status in (400, 422), 'invalid location batch was accepted')
        require(client.request(api, query={'id': fresh['id']})[0] == 404, 'invalid batch partially committed')
        print(f'PASS {label}: single/batch writes, cross-endpoint reads, locations, retries, atomic validation', flush=True)

    fresh = value()
    changed = {key: item for key, item in records[0].items() if key not in ('sequence', 'received_at')}
    changed['location_id'] = 8
    require(client.request(api, 'POST', [fresh, changed])[0] == 409, 'Go API did not reject location conflict')
    require(client.request(api, query={'id': fresh['id']})[0] == 404, 'conflict partially committed')
    for endpoint in (api, node_red):
        verify_list(client, endpoint, records, timeout)
    print(f'PASS: {len(records)} persisted observations verified through both endpoints and paginated lists; run={run_id}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', default='https://api.localhost')
    parser.add_argument('--node-red-url', default='https://flows.localhost')
    parser.add_argument('--node-red-backend', choices=('api', 'mqtt'), help='also assert the selected Node-RED write mode')
    parser.add_argument('--api-key-file', type=Path, default=ROOT / 'deploy/secrets/api_key')
    parser.add_argument('--cacert', type=Path, default=ROOT / 'deploy/certs/caddy-root.crt')
    parser.add_argument('--system-ca', action='store_true', help='use the system trust store instead of --cacert')
    parser.add_argument('--timeout', type=float, default=30, help='visibility/list deadline in seconds')
    args = parser.parse_args()
    try:
        require(shutil.which('curl') is not None, 'curl is required')
        require(0 < args.timeout <= 300, '--timeout must be between 0 and 300 seconds')
        for endpoint in (args.api_url, args.node_red_url):
            parts = urlsplit(endpoint)
            require(parts.scheme in ('http', 'https') and parts.hostname and not parts.username
                    and not parts.password and parts.path in ('', '/') and not parts.query and not parts.fragment,
                    'endpoint must be an HTTP(S) origin without credentials, path, query, or fragment')
            require(parts.scheme == 'https' or parts.hostname in ('localhost', '127.0.0.1', '::1'),
                    'plain HTTP is allowed only for local test services')
        require(args.api_url.rstrip('/') != args.node_red_url.rstrip('/'), 'Go API and Node-RED must use different origins')
        key = args.api_key_file.read_text(encoding='utf-8').rstrip('\r\n')
        require(len(key) >= 32 and all(33 <= ord(char) <= 126 for char in key), 'API key file must contain a printable key of at least 32 characters')
        cacert = None if args.system_ca else args.cacert
        require(cacert is None or cacert.is_file(), 'CA certificate missing; run make dev or supply --cacert/--system-ca')
        run_checks(Curl(key, cacert), args.api_url.rstrip('/'), args.node_red_url.rstrip('/'), args.timeout, args.node_red_backend)
        return 0
    except (CheckFailed, OSError, subprocess.TimeoutExpired) as error:
        # Do not echo child output or request/response bodies, which may contain secrets.
        message = str(error) if isinstance(error, CheckFailed) else 'local file or process operation failed'
        print('FAIL: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
