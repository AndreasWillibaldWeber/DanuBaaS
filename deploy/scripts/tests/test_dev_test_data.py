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
        self.assertEqual({r['location_id'] for r in self.rows}, {9000, 9001, 9002, 9003})
        self.assertTrue(all(type(r['location_id']) is int for r in self.rows))

    def test_history_and_alerts_share_four_stable_sensors(self):
        sensors = {row['sensor_id'] for row in self.rows}
        self.assertEqual(sensors, {'WL-002', 'WL-003', 'WL-004', 'WL-005'})
        for scenario, sensor in data.SCENARIOS.items():
            first = data.alert_rule('first-run', *scenario)
            self.assertEqual(first, data.alert_rule('second-run', *scenario))
            self.assertEqual(first['sensor_id'], sensor)
            self.assertIn(sensor, sensors)

    def test_scenario_behaviour(self):
        profiles = {p: [r['value'] for r in self.rows if r['metadata']['profile'] == p] for p in data.PROFILES}
        self.assertEqual(profiles['level-warning'][0], 1.4)
        self.assertEqual(profiles['level-warning'][-1], 2.4)
        self.assertEqual(profiles['level-critical'][0], 2.1)
        self.assertEqual(profiles['level-critical'][-1], 3.4)
        for profile in ('rise-warning', 'rise-critical'):
            self.assertEqual(profiles[profile][-1], .8)
            self.assertLess(max(profiles[profile]), 2)
        for values in profiles.values():
            self.assertLess(max(abs(b-a) for a,b in zip(values,values[1:])), .02)

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
        history = data.dataset(start - timedelta(hours=1))
        for live in report['observations'][:4]:
            previous = next(r for r in reversed(history) if r['sensor_id'] == live['sensor_id'])
            for field in ('value', 'unit', 'sensor_type', 'gateway_id', 'location_id', 'lon_lat'):
                self.assertEqual(live.get(field), previous.get(field), (live['sensor_id'], field))
            for field in ('profile', 'ingestion_route'):
                self.assertEqual(live['metadata'][field], previous['metadata'][field])
            self.assertGreater(live['timestamp'], previous['timestamp'])
        self.assertEqual(len(report['rules']), 4)
        self.assertEqual(len(report['observations']), 6)
        self.assertEqual({r['location_id'] for r in report['observations']}, {9000, 9001, 9002, 9003})
        for r in report['observations'][-2:]:
            baseline = next(b for b in report['observations'][:4] if b['sensor_id'] == r['sensor_id'])
            self.assertEqual(r['location_id'], baseline['location_id'])
        self.assertEqual(load.call_count, 2)
        self.assertEqual(len(verify.call_args.args[1]), 4)
        self.assertEqual({(r['id'].split('-')[-2], r['id'].split('-')[-1]) for r in report['rules']},
                         {('level', 'warning'), ('level', 'critical'), ('rise', 'warning'), ('rise', 'critical')})
        for row in report['observations'][-2:]:
            rate = (row['value'] - .8) / 5 * 60
            self.assertAlmostEqual(rate, .15 if row['sensor_id'] == 'WL-004' else .3)
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

    def test_alert_verification_rejects_unintended_extra_conditions(self):
        from unittest.mock import Mock
        expected = {('demo-rule', 'rise', 'warning'): 'reading'}
        alerts = [dict(id=1, rule_id='demo-rule', kind='rise', severity='warning'),
                  dict(id=2, rule_id='demo-rule', kind='level', severity='critical')]
        with patch.object(data, 'active_alerts', return_value=alerts), self.assertRaises(data.CheckFailed):
            data.verify_alerts(Mock(), expected, 0)

    def test_near_hour_history_leaves_live_baseline_window_before_rule_creation(self):
        from unittest.mock import Mock
        start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
        admin = Mock()
        admin.request.return_value = (403, {})
        with patch.object(data, 'datetime') as clock, patch.object(data.time, 'sleep') as sleep, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(data.CheckFailed):
            clock.now.side_effect = [start + timedelta(seconds=10),
                                     start + timedelta(seconds=40), start + timedelta(seconds=61)]
            data.load_alert_demo(Mock(), admin, 'api', 120, history_end=start)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 21])
        admin.request.assert_called_once()

    def test_rule_failure_does_not_submit_observations(self):
        from unittest.mock import Mock
        admin = Mock()
        admin.request.return_value = (403, {})
        with patch.object(data, 'load') as load, self.assertRaises(data.CheckFailed):
            data.load_alert_demo(Mock(), admin, 'api', 0)
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
