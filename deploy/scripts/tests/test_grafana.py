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

    def test_combined_chart_and_opening_evidence(self):
        dashboard = builder.dashboard()
        panels = {p['id']:p for p in dashboard['panels']}
        self.assertEqual([p['id'] for p in panels.values() if p['type']=='timeseries'], [1])
        self.assertEqual([v['name'] for v in dashboard['templating']['list']], ['sensor'])
        sql = panels[1]['targets'][0]['rawSql']
        self.assertIn("('minimum',d.minimum),('maximum',d.maximum),('value',d.value)",sql)
        self.assertIn("d.sensor_id || ':' || samples.kind",sql)
        sql = panels[3]['targets'][0]['rawSql']
        self.assertIn("event_type='opened'", sql)
        self.assertLess(sql.index('a.severity'), sql.index('value_at_trigger'))
        self.assertLess(sql.index('maximum_at_trigger'), sql.index('a.opened_at'))

    def test_coordinates_keep_six_decimal_places(self):
        observations = next(p for p in builder.dashboard()['panels'] if p['id']==2)
        overrides = {o['matcher']['options']:o['properties'] for o in observations['fieldConfig']['overrides']}
        for coordinate in ('longitude', 'latitude'):
            self.assertIn({'id':'decimals','value':6}, overrides[coordinate])
