#!/usr/bin/env python3
"""Exercise the real Caddyfile on loopback using a controlled upstream.

Set CADDY_BIN to a Caddy executable. These tests use HTTP on ephemeral loopback
ports; deployment continues to use HTTPS hostnames and automatic certificates.
"""
import base64
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

DEPLOY = Path(__file__).resolve().parents[1]


class Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/'):
            self.send_response(200 if self.headers.get('X-API-Key') == 'test-api-key' else 401)
        else:
            self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({
            'authorization': self.headers.get('Authorization'),
            'cookie': self.headers.get('Cookie'),
            'trusted_user': self.headers.get('X-WEBAUTH-USER'),
        }).encode())

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ProxyContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.thread.start()
        cls.work = tempfile.TemporaryDirectory(prefix='dbe-caddy-test-')
        cls.ports = {name: free_port() for name in ('API_HOST', 'GRAFANA_HOST', 'NODE_RED_HOST', 'NODE_RED_API_HOST')}
        env = os.environ.copy()
        env.update({name: 'http://127.0.0.1:' + str(port) for name, port in cls.ports.items()})
        password_hash = subprocess.check_output([
            'node', '-e', "process.stdout.write(require('./node_modules/bcryptjs').hashSync('outer-password',4))"
        ], cwd=DEPLOY / 'nodered', text=True)
        env.update(CADDY_USER='outer', CADDY_PASSWORD_HASH=password_hash,
                   XDG_DATA_HOME=cls.work.name, XDG_CONFIG_HOME=cls.work.name)
        config = (DEPLOY / 'caddy/Caddyfile').read_text()
        for peer in ('api:8080', 'grafana:3000', 'node-red:1880'):
            config = config.replace(peer, f'127.0.0.1:{cls.upstream.server_port}')
        file = Path(cls.work.name) / 'Caddyfile'
        file.write_text(config)
        cls.logs = open(Path(cls.work.name) / 'caddy.log', 'w+')
        cls.process = subprocess.Popen([os.environ['CADDY_BIN'], 'run', '--config', str(file), '--adapter', 'caddyfile'],
                                       env=env, stdout=cls.logs, stderr=cls.logs)
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection('127.0.0.1', cls.ports['API_HOST'], timeout=1)
                conn.request('GET', '/api/v1/values')
                conn.getresponse().read()
                conn.close()
                break
            except OSError:
                time.sleep(.05)
        else:
            cls.logs.seek(0)
            raise AssertionError('Caddy failed to start: ' + cls.logs.read())

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.logs.close()
        cls.work.cleanup()

    def get(self, host, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.ports[host], timeout=3)
        try:
            conn.request('GET', path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def outer(self):
        token = base64.b64encode(b'outer:outer-password').decode()
        return {'Authorization': 'Basic ' + token}

    def test_dashboards_require_outer_auth_even_with_application_cookie(self):
        for host in ('GRAFANA_HOST', 'NODE_RED_HOST'):
            self.assertEqual(self.get(host, '/dashboard', {'Cookie': 'session=valid'})[0], 401)

    def test_grafana_receives_cookie_but_no_outer_credentials_or_spoofed_user(self):
        headers = self.outer() | {'Cookie': 'grafana_session=example', 'X-WEBAUTH-USER': 'admin'}
        code, body = self.get('GRAFANA_HOST', '/login', headers)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIsNone(data['authorization'])
        self.assertIsNone(data['trusted_user'])
        self.assertEqual(data['cookie'], 'grafana_session=example')

    def test_node_red_cookie_is_preserved_and_editor_is_not_public(self):
        headers = self.outer() | {'Cookie': '__Host-dbe-dashboard=example'}
        code, body = self.get('NODE_RED_HOST', '/dashboard/_setup', headers)
        self.assertEqual(code, 200)
        self.assertIsNone(json.loads(body)['authorization'])
        self.assertEqual(json.loads(body)['cookie'], '__Host-dbe-dashboard=example')
        self.assertEqual(self.get('NODE_RED_HOST', '/admin/flows', headers)[0], 404)

    def test_machine_endpoints_use_api_keys_without_browser_basic_auth(self):
        for host in ('API_HOST', 'NODE_RED_API_HOST'):
            self.assertEqual(self.get(host, '/api/v1/values')[0], 401)
            self.assertEqual(self.get(host, '/api/v1/values', self.outer())[0], 401)
            self.assertEqual(self.get(host, '/api/v1/values', {'X-API-Key': 'test-api-key'})[0], 200)
            self.assertEqual(self.get(host, '/admin', {'X-API-Key': 'test-api-key'})[0], 404)


if __name__ == '__main__':
    if not os.environ.get('CADDY_BIN'):
        raise SystemExit('Set CADDY_BIN to the Caddy executable under test')
    unittest.main(verbosity=2)
