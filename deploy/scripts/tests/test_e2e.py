"""Test the E2E verifier's failure detection separately from deployment acceptance."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('e2e', Path(__file__).resolve().parents[1] / 'e2e.py')
e2e = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(e2e)


class ContractPeer:
    """Controlled peer for testing the verifier, not evidence of real persistence."""
    def __init__(self, mqtt=False, fault=None):
        self.records = {}
        self.pending = []
        self.mqtt = mqtt
        self.fault = fault
        self.polls = 0
        self.sequence = 0

    def request(self, endpoint, method='GET', body=None, query=None, authenticated=True):
        if not authenticated:
            return (200 if self.fault == 'auth' else 401), {}
        if method == 'POST':
            items = body if isinstance(body, list) else [body]
            normalized = [e2e.canonical(item) for item in items]
            if any(item.get('lon_lat') is not None and None in item['lon_lat'] for item in normalized):
                return 422, {}
            for item in normalized:
                previous = self.records.get(item['id'])
                if previous and {k: v for k, v in previous.items() if k not in ('sequence', 'received_at')} != item:
                    return 409, {}
            created = []
            for item in normalized:
                if item['id'] not in self.records:
                    self.sequence += 1
                    record = dict(item, sequence=self.sequence, received_at='2026-09-14T10:00:00Z')
                    self.records[item['id']] = record
                    created.append(record)
            result = [copy.deepcopy(self.records[item['id']]) for item in normalized]
            if self.mqtt and endpoint == 'node-red':
                for record in created:
                    self.pending.append(record)
                    del self.records[record['id']]
                return 202, {'status': 'accepted', 'ids': [item['id'] for item in items]}
            return (201 if created else 200), result if isinstance(body, list) else result[0]
        if query and 'id' in query:
            if self.fault == 'lost':
                return 404, {}
            if self.pending:
                self.polls += 1
                for record in self.pending:
                    self.records[record['id']] = record
                self.pending.clear()
                return 404, {}
            record = copy.deepcopy(self.records.get(query['id']))
            if record and self.fault == 'content':
                record['value'] = 99
            return (200, record) if record else (404, {})
        if self.fault == 'list':
            return 200, []
        if self.fault == 'cursor':
            return 200, [copy.deepcopy(next(iter(self.records.values())))]
        return 200, [copy.deepcopy(record) for record in sorted(self.records.values(), key=lambda item: item['sequence'])
                     if record['sequence'] > query['after']][:query['limit']]


class EndToEndVerifierTests(unittest.TestCase):
    def run_peer(self, peer, timeout=1):
        with contextlib.redirect_stdout(io.StringIO()):
            e2e.run_checks(peer, 'api', 'node-red', timeout)

    def test_synchronous_and_eventual_visibility_and_repeatability(self):
        for mqtt in (False, True):
            with self.subTest(mqtt=mqtt):
                peer = ContractPeer(mqtt=mqtt)
                self.run_peer(peer)
                self.run_peer(peer)
                self.assertEqual(len(peer.records), 24)
                if mqtt:
                    self.assertGreater(peer.polls, 0)

    def test_false_success_and_data_corruption_are_detected(self):
        for fault in ('auth', 'lost', 'content', 'list', 'cursor'):
            with self.subTest(fault=fault), self.assertRaises(e2e.CheckFailed):
                self.run_peer(ContractPeer(fault=fault), timeout=0)

    def test_timestamp_normalization_does_not_change_the_instant(self):
        for source, expected in [('2026-09-14T10:00:00.123400Z', '2026-09-14T10:00:00.1234Z'),
                                 ('2026-09-14T12:00:00.000000+02:00', '2026-09-14T10:00:00Z')]:
            self.assertEqual(e2e.canonical({'timestamp': source})['timestamp'], expected)

    def test_asserted_backend_detects_wrong_route(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(e2e.CheckFailed):
            e2e.run_checks(ContractPeer(), 'api', 'node-red', node_red_backend='mqtt')

    def test_curl_keeps_key_out_of_arguments_and_diagnostics(self):
        key = 'private-test-key-' * 4
        response = subprocess.CompletedProcess([], 60, stdout='', stderr=key)
        with patch.object(e2e.subprocess, 'run', return_value=response) as run:
            with self.assertRaises(e2e.CheckFailed) as failure:
                e2e.Curl(key, Path('/test/ca.crt')).request('https://example.test', 'POST', {'value': 0})
        command = run.call_args.args[0]
        self.assertNotIn(key, ' '.join(command))
        self.assertIn(key, run.call_args.kwargs['input'])
        self.assertNotIn(key, str(failure.exception))
        self.assertEqual(command[:2], ['curl', '--disable'])
        self.assertIn('--cacert', command)
        self.assertNotIn('--insecure', command)
        self.assertNotIn('--location', command)

    def test_bad_transport_and_non_json_responses_fail(self):
        for response in ('broken', 'redirect\n302', '<html>\n200'):
            with self.subTest(response=response), patch.object(e2e.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, stdout=response, stderr='')):
                with self.assertRaises(e2e.CheckFailed):
                    e2e.Curl('k' * 32, None).request('http://localhost')

    def test_cli_rejects_remote_plaintext_and_header_injection_before_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / 'key'
            for key, url in [('k' * 32, 'http://example.test'), ('k' * 32 + '\nInjected: value', 'https://example.test')]:
                secret.write_text(key)
                args = ['e2e.py', '--api-url', url, '--api-key-file', str(secret), '--system-ca']
                with patch('sys.argv', args), patch.object(e2e.Curl, 'request') as request, contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(e2e.main(), 1)
                    request.assert_not_called()
                    self.assertNotIn(key, output.getvalue())

    @unittest.skipUnless(shutil.which('curl'), 'curl is required for the transport test')
    def test_real_curl_sends_authenticated_json_and_does_not_follow_redirects(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.headers.get('X-API-Key'), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b'{"stored":true}')

            def do_GET(self):
                received.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/must-not-follow')
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = e2e.Curl('k' * 32, None)
            endpoint = f'http://127.0.0.1:{server.server_port}'
            self.assertEqual(client.request(endpoint, 'POST', {'value': 0}), (201, {'stored': True}))
            self.assertEqual(received[0], ('k' * 32, {'value': 0}))
            self.assertEqual(client.request(endpoint)[0], 302)
            self.assertEqual(received[1:], ['/api/v1/values'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
