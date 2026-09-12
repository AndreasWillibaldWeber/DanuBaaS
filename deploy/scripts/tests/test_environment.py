"""Bootstrap contract tests; all generated state stays in temporary directories."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('environment', Path(__file__).resolve().parents[1] / 'environment.py')
env = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(env)


class EnvironmentContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dbe-bootstrap-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.deploy = self.root / 'repo/deploy'
        self.deploy.mkdir(parents=True)
        self.secrets = self.root / 'private/secrets'
        self.secrets.mkdir(parents=True, mode=0o700)
        self.certs = self.root / 'mqtt'
        self.certs.mkdir()
        self.values = {key: 'operator' for key in env.USERS}
        self.values.update(dict(zip(env.WEB_HOSTS, ('api.dbe.org', 'grafana.dbe.org', 'dashboard.dbe.org', 'flows.dbe.org'))))
        self.values.update(MQTT_HOST='mqtt.dbe.org', INGESTION_BACKEND='mqtt',
                           SECRETS_DIR=str(self.secrets), MQTT_CERTS_DIR=str(self.certs))
        for index, name in enumerate(env.SECRET_NAMES):
            secret = ('$2b$12$' + chr(65 + index) * 53) if name.endswith('_hash') else os.urandom(32).hex()
            (self.secrets / name).write_text(secret + '\n')
        self.write_config()

    def write_config(self):
        (self.deploy / '.env.production').write_text(''.join(f'{key}={value}\n' for key, value in self.values.items()))

    def validate(self):
        with patch.object(env, 'check_certificate'):
            return env.validate_production(self.deploy)

    def test_complete_explicit_configuration_passes_without_mutation(self):
        before = {file: file.read_bytes() for file in self.root.rglob('*') if file.is_file()}
        self.assertEqual(self.validate(), self.values)
        self.assertEqual(before, {file: file.read_bytes() for file in self.root.rglob('*') if file.is_file()})

    def test_every_production_setting_is_required(self):
        for key in env.CONFIG_KEYS:
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = ''
                self.write_config()
                with self.assertRaises(env.ConfigurationError):
                    self.validate()
                self.values[key] = original

    def test_missing_empty_placeholder_reused_and_malformed_secrets_fail(self):
        target = self.secrets / 'api_key'
        for invalid in ('', 'short', 'replace-with-your-production-api-key', 'a' * 64, '秘密' * 16,
                        (self.secrets / 'db_admin_password').read_text(), ' leading' + os.urandom(32).hex()):
            with self.subTest(case=invalid[:8]):
                target.write_text(invalid)
                with self.assertRaises(env.ConfigurationError):
                    self.validate()
        target.unlink()
        with self.assertRaises(env.ConfigurationError):
            self.validate()

    def test_local_hosts_reused_hosts_relative_paths_and_dev_credentials_fail(self):
        for key, invalid in [('API_HOST', 'api.localhost'), ('API_HOST', 'https://api.dbe.org'),
                             ('API_HOST', self.values['GRAFANA_HOST']), ('SECRETS_DIR', 'deploy/secrets'),
                             ('SECRETS_DIR', str(self.deploy / 'secrets')), ('INGESTION_BACKEND', 'typo')]:
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = invalid
                self.write_config()
                with self.assertRaises(env.ConfigurationError):
                    self.validate()
                self.values[key] = original
        self.write_config()
        (self.secrets / 'operator-credentials.json').write_text('{}')
        with self.assertRaises(env.ConfigurationError):
            self.validate()

    def test_hashes_and_private_directory_are_checked(self):
        target = self.secrets / 'caddy_password_hash'
        original = target.read_text()
        for invalid in ('plaintext-is-not-a-hash', original.replace('$12$', '$04$')):
            target.write_text(invalid)
            with self.assertRaises(env.ConfigurationError):
                self.validate()
        target.write_text(original)
        self.secrets.chmod(0o755)
        with self.assertRaises(env.ConfigurationError):
            self.validate()

    def test_shell_interpolation_duplicates_and_unknown_keys_are_rejected(self):
        file = self.deploy / '.env.production'
        for text in ('API_HOST=${OTHER_HOST}\n', 'API_HOST=x\nAPI_HOST=y\n', 'TYPO=value\n'):
            file.write_text(text)
            with self.assertRaises(env.ConfigurationError):
                env.read_env(file)

    def test_production_fails_before_docker_or_generation_and_does_not_expose_secret(self):
        sensitive = 'replace-' + os.urandom(32).hex()
        (self.secrets / 'api_key').write_text(sensitive)
        output = io.StringIO()
        # Direct validation diagnostics must identify only the secret's name.
        with self.assertRaises(env.ConfigurationError) as caught:
            self.validate()
        self.assertNotIn(sensitive, str(caught.exception))
        with patch.object(env, 'DEPLOY', self.deploy), \
                patch.object(env, 'check_tools') as tools, patch.object(env, 'compose') as compose, \
                patch.object(env, 'prepare_development') as development, contextlib.redirect_stderr(output):
            self.assertEqual(env.main(['prod']), 1)
            tools.assert_not_called()
            compose.assert_not_called()
            development.assert_not_called()
        self.assertNotIn(sensitive, output.getvalue())

    def test_production_check_does_not_start_containers(self):
        with patch.object(env, 'validate_production'), patch.object(env, 'compose') as compose, \
                patch.object(env, 'check_tools') as tools, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(env.main(['prod-check']), 0)
            compose.assert_not_called()
            tools.assert_not_called()

    def test_compose_uses_separate_project_and_ignores_shell_overrides(self):
        with patch.dict(os.environ, {'API_HOST': 'unvalidated.dbe.org', 'COMPOSE_FILE': 'other.yaml'}):
            self.assertNotIn('API_HOST', env.process_env())
            self.assertNotIn('COMPOSE_FILE', env.process_env())
        with patch.object(env, 'run') as run:
            env.compose('prod', 'config', '--quiet')
            args = run.call_args.args[0]
            self.assertIn('dbe-sensors-production', args)
            self.assertIn(str(env.DEPLOY / '.env.production'), args)

    def test_development_preserves_existing_credentials_and_configuration(self):
        original_deploy = env.DEPLOY
        shutil.copyfile(original_deploy / '.env.example', self.deploy / '.env.example')
        (self.deploy / 'secrets').mkdir()
        (self.deploy / 'certs').mkdir()
        sentinel = self.deploy / 'secrets/api_key'
        sentinel.write_text('existing-do-not-rotate')
        with patch.object(env, 'DEPLOY', self.deploy), patch.object(env, 'run') as run, \
                patch.object(env, 'check_secrets'), patch.object(env, 'check_certificate'):
            env.prepare_development()
            env.prepare_development()
        self.assertEqual(sentinel.read_text(), 'existing-do-not-rotate')
        self.assertEqual((self.deploy / '.env').read_bytes(), (self.deploy / '.env.example').read_bytes())
        self.assertFalse(any(str(self.deploy / 'scripts/setup.mjs') in call.args[0] for call in run.call_args_list))

    def test_fresh_development_runs_generator_and_creates_configuration(self):
        shutil.copyfile(env.DEPLOY / '.env.example', self.deploy / '.env.example')
        with patch.object(env, 'DEPLOY', self.deploy), patch.object(env, 'run') as run, \
                patch.object(env, 'check_secrets'), patch.object(env, 'check_certificate'):
            env.prepare_development()
        self.assertTrue(any(str(self.deploy / 'scripts/setup.mjs') in call.args[0] for call in run.call_args_list))
        self.assertTrue((self.deploy / '.env').is_file())
        self.assertTrue((self.deploy / 'backups').is_dir())

    def test_development_tool_failure_causes_no_generation_or_start(self):
        with patch.object(env, 'check_tools', side_effect=env.ConfigurationError('Docker unavailable')), \
                patch.object(env, 'prepare_development') as prepare, patch.object(env, 'compose') as compose, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(env.main(['dev']), 1)
            prepare.assert_not_called()
            compose.assert_not_called()

    def test_production_start_validates_before_docker_and_never_seeds_data(self):
        events = []
        with patch.object(env, 'validate_production', side_effect=lambda: events.append('validate')), \
                patch.object(env, 'check_tools', side_effect=lambda **kwargs: events.append('tools')), \
                patch.object(env, 'compose', side_effect=lambda *args: events.append(args)), \
                patch.object(env, 'prepare_development') as prepare, patch.object(env, 'smoke_development') as smoke, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(env.main(['prod']), 0)
        self.assertEqual(events[:2], ['validate', 'tools'])
        self.assertEqual(events[-1], ('prod', 'up', '--build', '--wait', '--wait-timeout', '180'))
        prepare.assert_not_called()
        smoke.assert_not_called()


class SmokeContract(unittest.TestCase):
    def exercise(self, backend, *, never_visible=False, stale_flow=False, invalid_json=False):
        posts = []
        reads = []

        class Peer:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, method, resource, body=None, headers=None):
                self.method = method
                self.resource = resource
                self.body = body
                if method == 'POST':
                    posts.append(json.loads(body))
                else:
                    reads.append(resource)

            def getresponse(self):
                if self.method == 'POST':
                    self.status = 201 if backend == 'api' or stale_flow else 202
                else:
                    self.status = 404 if never_visible or len(reads) == 1 else 200
                return self

            def read(self):
                if self.status == 200 and invalid_json:
                    return b'not-json'
                return json.dumps(posts[-1] if self.status == 200 else {}).encode()

            def close(self):
                pass

        with patch.object(env, 'LocalHTTPSConnection', Peer), patch.object(env, 'compose'), \
                patch.object(env, 'read_secret', return_value='secret-not-logged'), \
                patch.object(env.ssl, 'create_default_context'), patch.object(env.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            if never_visible or stale_flow or invalid_json:
                with self.assertRaises(env.ConfigurationError):
                    env.smoke_development(backend)
                self.assertNotIn('Verified', output.getvalue())
            else:
                env.smoke_development(backend)
                self.assertIn('Verified', output.getvalue())
            self.assertNotIn('secret-not-logged', output.getvalue())
        self.assertEqual(len({value['id'] for value in posts}), 1, 'retries must preserve ID')
        return posts, reads

    def test_both_backends_wait_for_read_visibility_and_retain_retry_identity(self):
        for backend in ('api', 'mqtt'):
            with self.subTest(backend=backend):
                posts, reads = self.exercise(backend)
                self.assertEqual(len(posts), 2)
                self.assertEqual(len(reads), 2)

    def test_acceptance_without_database_visibility_is_failure(self):
        posts, _ = self.exercise('mqtt', never_visible=True)
        self.assertEqual(len(posts), 60)

    def test_stale_persisted_flow_does_not_report_the_wrong_backend_as_working(self):
        self.exercise('mqtt', stale_flow=True)

    def test_success_status_with_invalid_json_is_not_a_successful_smoke_check(self):
        self.exercise('api', invalid_json=True)


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is needed for real certificate checks')
class CertificateContract(unittest.TestCase):
    def test_real_certificate_matching_expiry_hostname_and_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(root / 'server.key'), '-out', str(root / 'server.crt'),
                            '-days', '2', '-subj', '/CN=mqtt.dbe.org', '-addext', 'subjectAltName=DNS:mqtt.dbe.org'],
                           check=True, capture_output=True)
            env.check_certificate(root, 'mqtt.dbe.org')
            with self.assertRaises(env.ConfigurationError):
                env.check_certificate(root, 'wrong.dbe.org')
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                            '-out', str(root / 'server.key')], check=True, capture_output=True)
            with self.assertRaises(env.ConfigurationError):
                env.check_certificate(root, 'mqtt.dbe.org')


            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(root / 'server.key'), '-out', str(root / 'server.crt'),
                            '-days', '1', '-subj', '/CN=mqtt.dbe.org', '-addext', 'subjectAltName=DNS:mqtt.dbe.org'],
                           check=True, capture_output=True)
            with self.assertRaises(env.ConfigurationError):
                env.check_certificate(root, 'mqtt.dbe.org')


@unittest.skipUnless(shutil.which('node') and shutil.which('openssl') and
                     (env.DEPLOY / 'nodered/node_modules/bcryptjs').is_dir(),
                     'Install Node-RED dependencies to exercise the real development generator')
class RealDevelopmentContract(unittest.TestCase):
    def test_generated_credentials_validate_and_rerun_preserves_every_secret(self):
        with tempfile.TemporaryDirectory(prefix='dbe-fresh-setup-') as directory:
            deploy = Path(directory) / 'deploy'
            (deploy / 'scripts').mkdir(parents=True)
            (deploy / 'nodered').mkdir()
            for name in ('setup.mjs', 'add-telegraf-secrets.mjs'):
                shutil.copyfile(env.DEPLOY / 'scripts' / name, deploy / 'scripts' / name)
            shutil.copyfile(env.DEPLOY / '.env.example', deploy / '.env.example')
            (deploy / 'nodered/node_modules').symlink_to(env.DEPLOY / 'nodered/node_modules', target_is_directory=True)
            real_run = env.run

            def run(args, **kwargs):
                if args[0] == 'npm':
                    return None  # Dependencies already installed; exercise real Node/OpenSSL setup.
                return real_run(args, capture=True)

            with patch.object(env, 'DEPLOY', deploy), patch.object(env, 'run', side_effect=run):
                env.prepare_development()
                original = {file.name: file.read_bytes() for file in (deploy / 'secrets').iterdir()}
                certificate = (deploy / 'certs/mqtt/server.crt').read_bytes()
                env.prepare_development()
            self.assertEqual(original, {file.name: file.read_bytes() for file in (deploy / 'secrets').iterdir()})
            self.assertEqual(certificate, (deploy / 'certs/mqtt/server.crt').read_bytes())
            self.assertEqual(len(original), len(env.SECRET_NAMES) + 1)
