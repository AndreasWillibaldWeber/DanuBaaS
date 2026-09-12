#!/usr/bin/env python3
"""Development bootstrap and explicit, read-only production preflight.

No third-party Python modules. Secrets never appear in subprocess arguments or
diagnostics. Production validation finishes before any Docker command is run.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

DEPLOY = Path(__file__).resolve().parents[1]
SECRET_NAMES = (
    'db_admin_password', 'db_api_password', 'db_grafana_password',
    'db_telegraf_password', 'api_key', 'mqtt_node_red_password',
    'mqtt_demo_password', 'mqtt_telegraf_password', 'node_red_credential_secret',
    'grafana_admin_password', 'node_red_admin_password_hash',
    'dashboard_password_hash', 'caddy_password_hash',
)
WEB_HOSTS = ('API_HOST', 'GRAFANA_HOST', 'NODE_RED_HOST', 'NODE_RED_API_HOST')
USERS = ('CADDY_USER', 'DASHBOARD_USER', 'NODE_RED_ADMIN_USER', 'GRAFANA_ADMIN_USER')
CONFIG_KEYS = (*WEB_HOSTS, *USERS, 'MQTT_HOST', 'INGESTION_BACKEND', 'SECRETS_DIR', 'MQTT_CERTS_DIR')
PLACEHOLDER = re.compile(r'changeme|change[-_ ]?me|replace|placeholder|your[-_ ]|example|todo', re.I)


class ConfigurationError(Exception):
    """A diagnostic safe to show to an operator."""


def read_env(path):
    if not path.is_file():
        raise ConfigurationError(f'Missing {path.name}; copy the matching .example file and configure it.')
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key not in CONFIG_KEYS or key in values:
            raise ConfigurationError(f'{path.name}:{number}: unknown, duplicate, or malformed setting')
        # Keep Python and Compose interpretation identical; values are not shell code.
        if value != value.strip() or any(c in value for c in '$\"\'`#'):
            raise ConfigurationError(f'{path.name}:{number}: use a literal, unquoted value without interpolation')
        values[key] = value
    return values


def process_env():
    # Shell values must not silently override the configuration we just validated.
    return {key: value for key, value in os.environ.items()
            if key not in CONFIG_KEYS and not key.startswith('COMPOSE_')}


def run(args, *, cwd=DEPLOY.parent, capture=False):
    return subprocess.run(args, cwd=cwd, env=process_env(), check=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def compose(mode, *args):
    production = mode == 'prod'
    return run(['docker', 'compose', '--project-name',
                'dbe-sensors-production' if production else 'dbe-sensors',
                '--env-file', str(DEPLOY / ('.env.production' if production else '.env')),
                '-f', str(DEPLOY / 'compose.yaml'), *args])


def check_tools(development):
    names = ['docker', 'openssl'] + (['node', 'npm'] if development else [])
    for name in names:
        if not shutil.which(name):
            raise ConfigurationError(f'Install {name} before starting this environment.')
    try:
        version = run(['docker', 'compose', 'version', '--short'], capture=True).stdout.decode().strip().lstrip('v')
        parts = tuple(int(n) for n in version.split('.')[:2])
        if parts < (2, 20):
            raise ConfigurationError('Docker Compose v2.20 or newer is required.')
        run(['docker', 'info'], capture=True)
        if development:
            node = run(['node', '--version'], capture=True).stdout.decode().strip().lstrip('v')
            if tuple(int(n) for n in node.split('.')[:2]) < (22, 9):
                raise ConfigurationError('Node.js 22.9 or newer is required.')
    except (subprocess.CalledProcessError, ValueError):
        raise ConfigurationError('Docker Compose or daemon unavailable; check Docker installation and daemon permissions.') from None


def read_secret(directory, name):
    file = directory / name
    try:
        if file.stat().st_size > 4096:
            raise ConfigurationError(f'{name}: secret exceeds 4096 bytes')
        value = file.read_text().removesuffix('\n')
    except (OSError, UnicodeError):
        raise ConfigurationError(f'{name}: required readable UTF-8 secret file is missing or invalid') from None
    if not value or value != value.strip() or any(c in value for c in '\r\n\x00'):
        raise ConfigurationError(f'{name}: secret must be nonempty, single-line, without surrounding whitespace')
    return value


def check_secrets(directory, production):
    if not directory.is_dir():
        raise ConfigurationError('SECRETS_DIR must be an existing directory.')
    if production and directory.stat().st_mode & 0o077:
        raise ConfigurationError('SECRETS_DIR must be private to its owner (chmod 700).')
    if production and (directory / 'operator-credentials.json').exists():
        raise ConfigurationError('Production must not reuse the development credential directory.')
    seen = set()
    for name in SECRET_NAMES:
        value = read_secret(directory, name)
        if name == 'api_key' and not value.isascii():
            raise ConfigurationError('api_key: use at least 32 ASCII bytes so Go and Node-RED interpret the key identically')
        if production and not (directory / name).stat().st_mode & 0o004:
            raise ConfigurationError(f'{name}: mounted secret must be readable by service UIDs; use mode 444 inside the private secrets directory')
        if name.endswith('_hash'):
            valid = re.fullmatch(r'\$2[aby]\$(1[2-9]|2[0-9]|3[01])\$[./A-Za-z0-9]{53}', value)
            if not valid:
                raise ConfigurationError(f'{name}: supply a bcrypt hash with cost 12 or higher')
        elif len(value.encode()) < 32 or (production and (PLACEHOLDER.search(value) or len(set(value)) < 8)):
            raise ConfigurationError(f'{name}: supply an independently generated secret of at least 32 bytes, not a placeholder')
        if production and value in seen:
            raise ConfigurationError(f'{name}: secrets must not be reused between services')
        seen.add(value)


def check_certificate(directory, host, production=False):
    certificate, key = directory / 'server.crt', directory / 'server.key'
    if not certificate.is_file() or not key.is_file():
        raise ConfigurationError('MQTT_CERTS_DIR must contain server.crt and server.key.')
    if production and (directory.stat().st_mode & 0o005 != 0o005
                       or any(not file.stat().st_mode & 0o004 for file in (certificate, key))):
        raise ConfigurationError('MQTT directory/files must be readable by the broker UID: use directory 755 and files 444 inside a private parent directory.')
    try:
        run(['openssl', 'x509', '-in', str(certificate), '-noout', '-checkend', '86400'], capture=True)
        public = run(['openssl', 'x509', '-in', str(certificate), '-pubkey', '-noout'], capture=True).stdout
        private_public = run(['openssl', 'pkey', '-in', str(key), '-passin', 'pass:', '-pubout'], capture=True).stdout
        if public != private_public:
            raise ConfigurationError('MQTT certificate and private key do not match.')
        # Also rejects certificates that are not yet valid. The supplied leaf is
        # the trust anchor here; external clients must use their operator's CA.
        hostname = ['-verify_hostname', host] if host else []
        run(['openssl', 'verify', '-partial_chain', '-trusted', str(certificate), *hostname, str(certificate)], capture=True)
    except subprocess.CalledProcessError:
        raise ConfigurationError('MQTT certificate/key invalid, expired, expires within 24 hours, or does not cover MQTT_HOST.') from None


def validate_production(deploy=None):
    deploy = deploy or DEPLOY
    values = read_env(deploy / '.env.production')
    for key in CONFIG_KEYS:
        if not values.get(key) or PLACEHOLDER.search(values[key]):
            raise ConfigurationError(f'{key}: explicit production configuration is required')
    if values['INGESTION_BACKEND'] not in ('api', 'mqtt'):
        raise ConfigurationError('INGESTION_BACKEND must be api or mqtt.')
    for key in (*WEB_HOSTS, 'MQTT_HOST'):
        host = values[key].lower()
        if (not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host)
                or host.endswith(('.localhost', '.local', '.test', '.invalid'))):
            raise ConfigurationError(f'{key}: supply a public DNS hostname without scheme or port')
    if len({values[key].lower() for key in WEB_HOSTS}) != len(WEB_HOSTS):
        raise ConfigurationError('The four HTTP hostnames must be distinct.')
    for key in USERS:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', values[key]):
            raise ConfigurationError(f'{key}: invalid username')
    for key in ('SECRETS_DIR', 'MQTT_CERTS_DIR'):
        directory = Path(values[key])
        if not directory.is_absolute() or directory.resolve().is_relative_to(deploy.parent.resolve()):
            raise ConfigurationError(f'{key}: use an absolute directory outside the repository')
    check_secrets(Path(values['SECRETS_DIR']), production=True)
    check_certificate(Path(values['MQTT_CERTS_DIR']), values['MQTT_HOST'], production=True)
    return values


def prepare_development():
    env_file = DEPLOY / '.env'
    if not env_file.exists():
        with env_file.open('x') as target:
            target.write((DEPLOY / '.env.example').read_text())
    values = read_env(env_file)
    defaults = read_env(DEPLOY / '.env.example')
    values = defaults | values
    if any(values.get(key, '') for key in ('SECRETS_DIR', 'MQTT_CERTS_DIR')):
        raise ConfigurationError('Development uses deploy/secrets and deploy/certs/mqtt; remove production directory settings.')
    if any(values[key] != defaults[key] for key in WEB_HOSTS):
        raise ConfigurationError('make dev requires the four .localhost hostnames; use make prod for public hosts.')
    if values['INGESTION_BACKEND'] not in ('api', 'mqtt'):
        raise ConfigurationError('INGESTION_BACKEND must be api or mqtt.')
    run(['npm', 'ci', '--prefix', str(DEPLOY / 'nodered'), '--no-audit', '--no-fund'])
    if not (DEPLOY / 'secrets').exists() and not (DEPLOY / 'certs').exists():
        run(['node', str(DEPLOY / 'scripts/setup.mjs')])
    elif not (DEPLOY / 'secrets').is_dir() or not (DEPLOY / 'certs').is_dir():
        raise ConfigurationError('Incomplete development setup: restore missing secrets/certificates; existing credentials were preserved.')
    else:
        run(['node', str(DEPLOY / 'scripts/add-telegraf-secrets.mjs')])
    check_secrets(DEPLOY / 'secrets', production=False)
    check_certificate(DEPLOY / 'certs/mqtt', 'mqtt.localhost')
    (DEPLOY / 'backups').mkdir(exist_ok=True)
    return values


class LocalHTTPSConnection(http.client.HTTPSConnection):
    """Connect locally while verifying Caddy's actual DNS certificate name."""
    def connect(self):
        transport = socket.create_connection(('127.0.0.1', self.port), self.timeout)
        self.sock = self._context.wrap_socket(transport, server_hostname=self.host)


def smoke_development(backend='api'):
    ca = DEPLOY / 'certs/caddy-root.crt'
    for attempt in range(60):
        try:
            compose('dev', 'cp', 'caddy:/data/caddy/pki/authorities/local/root.crt', str(ca))
            break
        except subprocess.CalledProcessError:
            if attempt == 59:
                raise ConfigurationError('Caddy did not create its local CA within 60 seconds.') from None
            time.sleep(1)
    context = ssl.create_default_context(cafile=str(ca))
    api_key = read_secret(DEPLOY / 'secrets', 'api_key')
    observation = dict(id=str(uuid.uuid4()), sensor_id='quickstart', gateway_id='demo',
                       sensor_type='water-level', timestamp=datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                       value=1.42, unit='m', metadata={'source': 'make dev'})
    body = json.dumps(observation).encode()
    def request(method, resource, data=None):
        connection = LocalHTTPSConnection('flows.localhost', 443, context=context, timeout=3)
        try:
            connection.request(method, resource, body=data,
                               headers={'X-API-Key': api_key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()
    # Retry the same ID: transient failures and asynchronous MQTT delivery cannot
    # create duplicates. Polling also verifies the read path reaches the database.
    for attempt in range(60):
        try:
            status, _ = request('POST', '/api/v1/values', body)
            if status in (200, 201, 202):
                if (status == 202) != (backend == 'mqtt'):
                    raise ConfigurationError('Saved Node-RED flows do not match INGESTION_BACKEND; import the updated demo flow as described in deploy/README.md.')
                status, payload = request('GET', '/api/v1/values?id=' + observation['id'])
                if status == 200:
                    try:
                        record = json.loads(payload)
                    except (ValueError, UnicodeError):
                        raise ConfigurationError('Development GET returned invalid JSON; inspect Node-RED logs.') from None
                    if isinstance(record, dict) and record.get('id') == observation['id']:
                        print('Verified Node-RED POST → TimescaleDB → GET with a fresh sample observation.')
                        return
            elif status not in (404, 502, 503, 504):
                raise ConfigurationError(f'Development POST failed with HTTP {status}; inspect Node-RED logs.')
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(1)
    raise ConfigurationError('Development write/read did not succeed; inspect node-red, api, and telegraf logs. Volumes and credentials were preserved.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('dev', 'prod', 'prod-check'))
    mode = parser.parse_args(argv).mode
    try:
        if mode in ('prod', 'prod-check'):
            validate_production()
            print('Production configuration, secret files, and MQTT certificate checks passed.')
            if mode == 'prod-check':
                return 0
        check_tools(development=mode == 'dev')
        if mode == 'dev':
            values = prepare_development()
        compose(mode, 'config', '--quiet')
        compose(mode, 'up', '--build', '--wait', '--wait-timeout', '180')
        if mode == 'dev':
            smoke_development(values['INGESTION_BACKEND'])
            print('Grafana: https://grafana.localhost | Node-RED: https://nodered.localhost/dashboard')
            print('Editor: http://127.0.0.1:1880/admin')
            print('Credentials: deploy/secrets/operator-credentials.json (values are never printed)')
            print('Trust deploy/certs/caddy-root.crt in your browser/OS. See QUICKSTART.md for login names.')
        else:
            print('Production services started. Project: dbe-sensors-production. No credentials were generated.')
        return 0
    except (ConfigurationError, OSError, subprocess.CalledProcessError) as error:
        # Never echo subprocess stderr: external commands may include configuration.
        detail = str(error) if isinstance(error, ConfigurationError) else 'A required command or file operation failed; check the preceding output and file permissions.'
        print('Setup failed: ' + detail, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
