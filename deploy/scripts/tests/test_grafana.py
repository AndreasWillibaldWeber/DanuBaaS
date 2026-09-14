"""Provisioned dashboard contract; SQL behaviour is checked by make test-grafana."""
import importlib.util
import json
from pathlib import Path
import unittest

PATH=Path(__file__).resolve().parents[2]/'grafana/build_dashboard.py'
SPEC=importlib.util.spec_from_file_location('dashboard_builder',PATH)
builder=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class DashboardContract(unittest.TestCase):
    def test_provisioned_dashboard_matches_source(self):
        self.assertEqual(json.loads(builder.OUTPUT.read_text()),builder.dashboard())

    def test_selection_applies_to_every_panel_with_escaped_sql_values(self):
        d=builder.dashboard()
        selector=d['templating']['list'][0]
        self.assertTrue(selector['includeAll'])
        self.assertFalse(selector['multi'])
        self.assertEqual(selector['current']['value'],'$__all')
        self.assertNotIn('allValue',selector)  # All expands actual IDs through sqlstring.
        for p in d['panels']:
            with self.subTest(panel=p['title']):
                self.assertIn('${sensor:sqlstring}',p['targets'][0]['rawSql'])
                self.assertNotIn('${sensor:raw}',p['targets'][0]['rawSql'])

    def test_four_cards_use_matching_severity_colours_and_left_right_layout(self):
        cards=[p for p in builder.dashboard()['panels'] if p['type']=='canvas']
        self.assertEqual(len(cards),4)
        self.assertEqual([p['gridPos']['x'] for p in cards],[0,6,12,18])
        for p,colour in zip(cards,['#A65300','#A82020','#A65300','#A82020']):
            root = p['options']['root']
            self.assertFalse(p['options']['inlineEditing'])
            self.assertEqual(root['background']['color']['fixed'], colour)
            elements = {e['name']:e for e in root['elements']}
            self.assertLess(elements['Current value']['placement']['left'],
                            elements['Limit value']['placement']['left'])
            self.assertLess(elements['Current heading']['placement']['top'],
                            elements['Current station']['placement']['top'])
            sql=p['targets'][0]['rawSql']
            self.assertIn("WHEN sensor_id IS NULL THEN '—'",sql)
            self.assertIn("WHEN current_value IS NULL THEN '-.-- m",sql)
