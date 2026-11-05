"""Five-level legacy-format training export, without a browser or model requests."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from clean_export import clean_hierarchy, export_clean
from rules import validate
from test_postprocess import node, tree, png


def fixture():
    """Depth-six source with one early-stopping sibling and diagnostic-only archive."""
    source = tree([node('l1'), node('l2', 'l1', [0, 0, 90, 90]),
        node('l3', 'l2', [0, 0, 80, 80]), node('l4', 'l3', [0, 0, 70, 70]),
        node('l5', 'l4', [0, 0, 60, 60]), node('l6', 'l5', [0, 0, 50, 50]),
        node('early', 'l1', [90, 0, 10, 10])])
    source.update(detail_archive={'nodes': [node('not_a_fifth_node')]},
        accumulation={'debug': True}, region_replacements=[{'debug': True}])
    source['nodes'][3]['accumulation_source'] = {'debug': True}
    source['capture']['url'] = 'https://example.com/page'
    return source


class CleanExportTest(unittest.TestCase):
    def test_real_five_levels_without_archive_flattening(self):
        source = fixture(); frozen = deepcopy(source)
        clean = clean_hierarchy(source)
        self.assertEqual(source, frozen)
        ids = {n['node_id'] for n in clean['nodes']}
        self.assertEqual(ids, {'l1', 'l2', 'l3', 'l4', 'l5', 'early'})
        self.assertEqual(validate(clean, 5)['l5'], 4)
        self.assertEqual(clean['stats']['by_layer'], {'0': 1, '1': 2, '2': 1, '3': 1, '4': 1})
        self.assertEqual(clean['capture']['url'], 'https://example.com/page')
        self.assertFalse({'detail_archive', 'accumulation', 'region_replacements'} & clean.keys())
        self.assertNotIn('accumulation_source', next(n for n in clean['nodes'] if n['node_id'] == 'l4'))
        self.assertEqual(clean['nodes'][4]['bbox'], source['nodes'][4]['bbox'])

    def make_run(self, root, status='accepted'):
        """Write canonical synthetic processing output; no paid service is involved."""
        run = root/'run'; observation = run/'project'/'desktop'; observation.mkdir(parents=True)
        (observation/'fused-hierarchy.json').write_text(json.dumps(fixture()))
        (observation/'layer-review.json').write_text(json.dumps({'status': status}))
        (run/'layer-postprocess-report.json').write_text(json.dumps({'observations': [{}]}))
        png(observation/'full.png')
        return run

    def test_only_png_and_legacy_hierarchy_are_written(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run = self.make_run(root); out = root/'clean'
            summary = export_clean(run, out)
            self.assertEqual(summary['observations'], 1)
            self.assertEqual({f.relative_to(out).as_posix() for f in out.rglob('*') if f.is_file()},
                {'pages/project/desktop/full.png', 'pages/project/desktop/hierarchy.json'})
            validate(json.loads((out/'pages/project/desktop/hierarchy.json').read_text()), 5)
            with self.assertRaisesRegex(ValueError, 'empty'):
                export_clean(run, out)

    def test_failed_or_incomplete_runs_do_not_partially_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run = self.make_run(root, 'needs_review'); out = root/'clean'
            with self.assertRaisesRegex(ValueError, 'accepted'):
                export_clean(run, out)
            self.assertFalse(out.exists())
            (run/'project/desktop/layer-review.json').write_text(json.dumps({'status': 'accepted'}))
            (run/'layer-postprocess-report.json').write_text(json.dumps({'observations': [{}, {}]}))
            with self.assertRaisesRegex(ValueError, 'Missing'):
                export_clean(run, out)
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
