"""Dataset reproducibility and loader failure detection; no real persistence implied."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_test_data as data
from test_e2e import ContractPeer


class Peer(ContractPeer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writes = []

    def request(self, endpoint, method='GET', body=None, **kwargs):
        route = 'node-red' if endpoint == data.NODE_RED else 'api'
        if method == 'POST':
            self.writes.append((route, copy.deepcopy(body)))
        return super().request(route, method, body, **kwargs)


class TestDataset(unittest.TestCase):
    def setUp(self):
        self.anchor = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
        self.rows = data.dataset(self.anchor)

    def test_reproducible_distinct_ids_and_complete_day(self):
        self.assertEqual(self.rows, data.dataset(self.anchor))
        self.assertEqual(len(self.rows), 580)
        ids = {row['id'] for row in self.rows}
        self.assertEqual(len(ids), 580)
        self.assertTrue(ids.isdisjoint({r['id'] for r in data.dataset(self.anchor + timedelta(hours=1))}))
        for profile in data.PROFILES:
            rows = [r for r in self.rows if r['metadata']['profile'] == profile]
            times = [datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')) for r in rows]
            self.assertEqual(times[0], self.anchor - timedelta(days=1))
            self.assertEqual(times[-1], self.anchor)
            self.assertTrue(all(b - a == timedelta(minutes=10) for a, b in zip(times, times[1:])))
            self.assertTrue(all(r['metadata']['synthetic'] and math.isfinite(r['value']) and r['value'] >= 0 for r in rows))
        self.assertEqual({r['metadata']['ingestion_route'] for r in self.rows}, {'api', 'node-red'})
        self.assertTrue(any('lon_lat' in r for r in self.rows))
        self.assertTrue(any('location_id' in r for r in self.rows))

    def test_scenario_behaviour(self):
        profiles = {p: [r['value'] for r in self.rows if r['metadata']['profile'] == p] for p in data.PROFILES}
        self.assertLess(max(profiles['stable']) - min(profiles['stable']), .06)
        self.assertGreater(max(profiles['fluctuating']) - min(profiles['fluctuating']), .7)
        self.assertLess(profiles['elevated'][0], 3)
        self.assertGreater(profiles['elevated'][-1], 3)
        self.assertAlmostEqual(profiles['rapid-rise'][-1] - profiles['rapid-rise'][-4], 1.2)

    def test_anchor_validation_and_timezone_equivalence(self):
        self.assertEqual(data.anchor_time(now=self.anchor + timedelta(minutes=42)), self.anchor)
        self.assertEqual(data.anchor_time('2026-09-14T14:00:00+02:00', self.anchor), self.anchor)
        for value in ('bad', '2026-09-14', '2026-09-14T12:00:01Z', '0001-01-01T00:00:00Z'):
            with self.subTest(value=value), self.assertRaises(data.CheckFailed):
                data.anchor_time(value, self.anchor)

    def test_both_backends_single_batch_and_safe_replay(self):
        for mqtt in (False, True):
            with self.subTest(mqtt=mqtt), contextlib.redirect_stdout(io.StringIO()):
                peer = Peer(mqtt=mqtt)
                counts = data.load(peer, self.rows, 'mqtt' if mqtt else 'api')
                before = copy.deepcopy(peer.records)
                data.load(peer, self.rows, 'mqtt' if mqtt else 'api')
                self.assertEqual(counts, {'api': 290, 'node-red': 290})
                self.assertEqual(peer.records, before)
                self.assertEqual(len(peer.records), 580)
                for route in ('api', 'node-red'):
                    bodies = [b for r, b in peer.writes if r == route]
                    self.assertTrue(any(isinstance(b, dict) for b in bodies))
                    self.assertTrue(any(isinstance(b, list) for b in bodies))
                    self.assertTrue(all(len(b) <= 100 for b in bodies if isinstance(b, list)))

    def test_lost_corrupted_or_wrong_backend_responses_fail(self):
        for fault in ('lost', 'content'):
            with self.subTest(fault=fault), self.assertRaises(data.CheckFailed):
                data.load(Peer(fault=fault), self.rows, 'api', timeout=0)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(data.CheckFailed):
            data.load(Peer(mqtt=True), self.rows, 'api')

    def test_invalid_local_configuration_fails_before_loading(self):
        with patch.object(data, 'read_env', return_value={'API_HOST': 'production.example'}), \
                patch.object(data, 'load') as load, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(data.main([]), 1)
            load.assert_not_called()

class TestAlertDemo(unittest.TestCase):
    def test_rules_and_live_readings_cover_all_four_conditions(self):
        from unittest.mock import Mock
        start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
        admin = Mock()
        admin.request.side_effect = lambda endpoint, method, rule, **kw: (201, {**rule, 'version': 1})
        with patch.object(data, 'datetime') as clock, patch.object(data.time, 'sleep'), \
                patch.object(data, 'load') as load, patch.object(data, 'verify_alerts', return_value=[]) as verify, \
                contextlib.redirect_stdout(io.StringIO()):
            clock.now.side_effect = [start, start + timedelta(seconds=5)]
            report = data.load_alert_demo(Mock(), admin, 'mqtt', 120)
        self.assertEqual(len(report['rules']), 4)
        self.assertEqual(len(report['observations']), 6)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(len(verify.call_args.args[1]), 4)
        self.assertEqual({(r['id'].split('-')[-2], r['id'].split('-')[-1]) for r in report['rules']},
                         {('level', 'warning'), ('level', 'critical'), ('rise', 'warning'), ('rise', 'critical')})
        for row in report['observations'][-2:]:
            rate = (row['value'] - .8) / 5 * 60
            self.assertAlmostEqual(rate, .15 if row['sensor_id'].endswith('warning') else .3)
        for call in admin.request.call_args_list:
            self.assertEqual(call.args[1], 'POST')
            self.assertEqual(call.kwargs['resource'], '/api/v1/alert-rules')
            self.assertEqual(call.args[2]['version'], 0)

    def test_alert_verification_rejects_wrong_severity_missing_evidence_and_timeout(self):
        from unittest.mock import Mock
        expected = {('demo-rule', 'rise', 'critical'): 'observation-id'}
        alert = dict(id=1, rule_id='demo-rule', kind='rise', severity='critical')
        client = Mock()
        client.request.return_value = (200, [{'details': {'observation_id': 'observation-id'}}])
        with patch.object(data, 'active_alerts', return_value=[alert]):
            self.assertEqual(data.verify_alerts(client, expected, 0), [alert])
            client.request.return_value = (200, [{'details': {'observation_id': 'unrelated'}}])
            with self.assertRaises(data.CheckFailed):
                data.verify_alerts(client, expected, 0)
        for alerts in ([], [{**alert, 'severity': 'warning'}]):
            with patch.object(data, 'active_alerts', return_value=alerts), self.assertRaises(data.CheckFailed):
                data.verify_alerts(client, expected, 0)

    def test_rule_failure_does_not_submit_observations(self):
        from unittest.mock import Mock
        admin = Mock()
        admin.request.return_value = (403, {})
        with patch.object(data, 'load') as load, self.assertRaises(data.CheckFailed):
            data.load_alert_demo(Mock(), admin, 'api', 0)
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
