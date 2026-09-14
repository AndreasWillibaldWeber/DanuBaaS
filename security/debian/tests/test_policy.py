"""Non-root validation and deployment-controller contract tests; no host changes."""
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
render = module('render')
firewall = module('firewall')


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'host.json'
        self.config = json.loads((ROOT / 'host.example.json').read_text())

    def load(self):
        self.file.write_text(json.dumps(self.config))
        return render.load(self.file)

    def test_example_and_transport_policy(self):
        policy = render.firewall(self.load())
        self.assertNotIn('flush ruleset', policy)
        self.assertIn('type filter hook forward priority -10; policy drop', policy)
        self.assertIn('ct status dnat ct original proto-dst { 80, 443 }', policy)
        self.assertIn('ct original proto-dst 8883', policy)
        self.assertIn('ip6 saddr', policy)
        self.assertIn('ipv6-icmp', policy)
        self.assertNotIn('dport 1880', policy)
        self.assertNotIn('dport 5432', policy)
        self.assertNotIn('udp dport 443', policy)
        self.assertIn('iifname "br-*" accept', policy)

    def test_untrusted_configuration_cannot_inject_rules(self):
        for key, value in [('external_interfaces', ['eth0"; flush ruleset']),
                           ('admin_users', ['root']), ('admin_users', ['operator\nPermitRootLogin yes']),
                           ('admin_networks', ['0.0.0.0/0']), ('admin_networks', ['::/0']),
                           ('admin_networks', ['192.0.2.3/24']),
                           ('mqtt_networks', []), ('ssh_port', True), ('ssh_port', 443),
                           ('dhcp4', 'false')]:
            with self.subTest(key=key, value=value):
                original = self.config[key]
                self.config[key] = value
                with self.assertRaises(ValueError):
                    self.load()
                self.config[key] = original

    def test_unknown_duplicate_and_missing_keys_fail(self):
        self.file.write_text('{"ssh_port":22,"ssh_port":23}')
        with self.assertRaises(ValueError):
            render.load(self.file)
        self.config['TYPO'] = True
        with self.assertRaises(ValueError):
            self.load()

    def test_ipv4_only_management_and_dhcp(self):
        self.config.update(admin_networks=['192.0.2.10/32'], dhcp4=True, dhcp6=True)
        policy = render.firewall(self.load())
        self.assertNotIn('ip6 saddr { 2001:db8', policy)
        self.assertIn('udp sport 67 udp dport 68', policy)
        self.assertIn('ip6 saddr fe80::/10 udp sport 547', policy)

    def test_ssh_keeps_only_the_required_editor_tunnel(self):
        files = render.artifacts(self.load())
        ssh = files['00-danubaas.conf']
        self.assertIn('AuthenticationMethods publickey', ssh)
        self.assertIn('PermitRootLogin no', ssh)
        self.assertIn('PermitOpen 127.0.0.1:1880 localhost:1880', ssh)
        self.assertNotIn('Ciphers', ssh)
        sysctl = files['60-danubaas.conf']
        self.assertNotIn('ip_forward =', sysctl)
        self.assertNotIn('disable_ipv6', sysctl)
        self.assertIn('Automatic-Reboot "false"', files['52danubaas-unattended-upgrades'])
        self.assertIn('nftables-multiport', files['danubaas.local'])

    def test_rollback_only_restores_managed_table(self):
        state = Path(self.tmp.name)
        (state / 'pending').write_text('trial')
        (state / 'previous.nft').write_text(firewall.PREFIX)
        with patch.object(firewall, 'STATE', state), patch.object(firewall, 'run') as run:
            firewall.restore()
            run.assert_called_once_with(firewall.NFT, '-f', str(state / 'previous.nft'))
            self.assertFalse((state / 'pending').exists())
            firewall.restore()
            self.assertEqual(run.call_count, 1)

    def test_failed_restore_keeps_pending_recovery_state(self):
        state = Path(self.tmp.name)
        (state / 'pending').write_text('trial')
        with patch.object(firewall, 'STATE', state), patch.object(firewall, 'run', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                firewall.restore()
        self.assertTrue((state / 'pending').exists())

    def test_confirm_without_trial_does_not_write(self):
        with patch.object(firewall, 'STATE', Path(self.tmp.name)), patch.object(firewall, 'write') as write:
            with self.assertRaises(ValueError):
                firewall.confirm()
            write.assert_not_called()

    def trial(self, fail_apply=False):
        self.config.update(admin_networks=['198.18.0.2/32'], external_interfaces=['eth0'])
        self.load()
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            if fail_apply and args[:2] == (firewall.NFT, '-f') and args[2].endswith('candidate.nft'):
                raise subprocess.CalledProcessError(1, args)
            return SimpleNamespace(stdout='')
        with patch.object(firewall, 'STATE', Path(self.tmp.name)), \
                patch.dict(sys.modules, {'render': render}), \
                patch.dict(os.environ, {'SSH_CONNECTION': ''}), \
                patch.object(firewall.Path, 'exists', return_value=True), \
                patch.object(firewall, 'snapshot', return_value=firewall.PREFIX), \
                patch.object(firewall, 'run', side_effect=run), \
                patch.object(firewall.subprocess, 'run', return_value=SimpleNamespace(returncode=3)):
            if fail_apply:
                with self.assertRaises(subprocess.CalledProcessError):
                    firewall.try_policy(self.file, True)
            else:
                firewall.try_policy(self.file, True)
        return calls

    def test_trial_arms_recovery_before_loading_firewall(self):
        calls = self.trial()
        timer = next(i for i, args in enumerate(calls) if args[0] == 'systemd-run')
        apply = next(i for i, args in enumerate(calls) if args[:2] == (firewall.NFT, '-f'))
        self.assertLess(timer, apply)
        self.assertTrue((Path(self.tmp.name) / 'pending').exists())
        self.assertTrue((Path(self.tmp.name) / 'controller.py').exists())

    def test_failed_trial_restores_runtime_and_clears_pending(self):
        calls = self.trial(fail_apply=True)
        self.assertEqual(calls[-1], (firewall.NFT, '-f', str(Path(self.tmp.name) / 'previous.nft')))
        self.assertFalse((Path(self.tmp.name) / 'pending').exists())

    def confirmation(self, fail=False):
        state = Path(self.tmp.name)
        persist = state / 'persistent'
        persist.mkdir()
        service = state / 'firewall.service'
        (persist / 'firewall.nft').write_text('old persisted rules')
        service.write_text('old service')
        (state / 'pending').write_text('trial')
        (state / 'candidate.nft').write_text('new rules')
        (state / 'candidate.service').write_text('new service')
        (state / 'previous.nft').write_text(firewall.PREFIX)
        def run(*args, **kwargs):
            if fail and args[:2] == ('systemctl', 'enable'):
                raise subprocess.CalledProcessError(1, args)
            return SimpleNamespace(stdout='')
        with patch.object(firewall, 'STATE', state), patch.object(firewall, 'PERSIST', persist), \
                patch.object(firewall, 'SERVICE', service), patch.object(firewall, 'private_directory'), \
                patch.object(firewall, 'run', side_effect=run), \
                patch.object(firewall.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            if fail:
                with self.assertRaises(subprocess.CalledProcessError):
                    firewall.confirm()
            else:
                firewall.confirm()
        return state, persist, service

    def test_confirmation_preserves_backup_and_commits_persistence(self):
        state, persist, service = self.confirmation()
        self.assertEqual((persist / 'firewall.nft').read_text(), 'new rules')
        self.assertEqual((persist / 'firewall.nft.previous').read_text(), 'old persisted rules')
        self.assertEqual(service.read_text(), 'new service')
        self.assertFalse((state / 'pending').exists())

    def test_confirmation_failure_restores_persistence_and_runtime(self):
        state, persist, service = self.confirmation(fail=True)
        self.assertEqual((persist / 'firewall.nft').read_text(), 'old persisted rules')
        self.assertEqual(service.read_text(), 'old service')
        self.assertFalse((state / 'pending').exists())


if __name__ == '__main__':
    unittest.main()
