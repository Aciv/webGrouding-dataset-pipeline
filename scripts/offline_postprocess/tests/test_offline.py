"""Small local fixtures verify graph, protocol, loss eligibility and real artifact publication."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image, ImageDraw
from scripts.offline_postprocess.config import load_config
from scripts.offline_postprocess.domain import normalize, graph_depths
from scripts.offline_postprocess.cleanup import clean_nodes
from scripts.offline_postprocess.slicing import plan_cuts, crop_views
from scripts.offline_postprocess.application import run_dataset
from scripts.offline_postprocess.statistics import iou_statistics, compute_statistics


def node(key, box, parent=None, struct='text', subtype='text', **extra):
    """Build a source fixture in CSS xywh, with explicit original detection eligibility."""
    return {'node_id': key, 'parent_id': parent, 'bbox': box, 'struct': struct, 'subtype': subtype,
            'disposition': 'detection', 'source_node_ids': [key], **extra}


def source(nodes, height=8000, width=390):
    """Build a raw candidate source, never a legacy postprocessed fixture."""
    return {'rule_version': 'dom_visual_rules_6_child_geometry_offline', 'capture': {
        'width': width, 'height': height, 'image': 'full.png'}, 'nodes': nodes, 'ignore_regions': []}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_depth_and_visibility_are_independent_of_detection(self):
        raw = source([node(str(i), [0, i*30, 100, 25], str(i-1) if i else None,
                           aria_hidden=True) for i in range(9)])
        nodes, _, _ = normalize(raw, 'page/mobile')
        self.assertEqual(nodes[-1]['derived_depth'], 8)
        self.assertTrue(nodes[-1]['detection_eligible'])
        self.assertEqual(nodes[-1]['pixel_visibility'], 'unknown')
        self.assertEqual(raw['nodes'][0]['bbox'], [0, 0, 100, 25])
        raw['rule_version'] = 'visual_layers_fusion_clean5_v13'
        with self.assertRaisesRegex(ValueError, 'obsolete'):
            normalize(raw, 'page/mobile')

    def test_graph_rejects_cycles_missing_and_duplicate_ids(self):
        for nodes in ([node('a', [0, 0, 1, 1], 'b')],
                      [node('a', [0, 0, 1, 1], 'b'), node('b', [0, 0, 1, 1], 'a')],
                      [node('a', [0, 0, 1, 1]), node('a', [0, 0, 1, 1])]):
            with self.assertRaises(ValueError):
                graph_depths(nodes)

    def test_component_representation_does_not_drop_heading_or_button(self):
        raw = source([node('card', [0, 0, 300, 200], struct='block', subtype='article'),
                      node('date', [0, 100, 60, 16], 'card'), node('author', [0, 120, 60, 16], 'card'),
                      node('title', [0, 0, 120, 20], 'card', text_role='heading'),
                      node('button', [0, 150, 16, 16], 'card', struct='control', subtype='button')])
        normalized, w, h = normalize(raw, 'page/mobile')
        nodes, ops, _ = clean_nodes(normalized, w, h, self.config['cleanup'], 1800)
        index = {n['node_id']: n for n in nodes}
        self.assertFalse(index['date']['detection_eligible'])
        self.assertEqual(index['date']['represented_by'], 'page/mobile/card')
        self.assertTrue(index['title']['detection_eligible'])
        self.assertTrue(index['button']['detection_eligible'])
        self.assertEqual(len(raw['nodes']), 5)
        self.assertTrue(ops)

    def test_height_boundaries_and_no_lost_tail(self):
        for height in [899, 900, 1800, 1801, 2700, 2701, 8000]:
            cuts, trace = plan_cuts(390, height, [], [], self.config['slicing'])
            crops, missing = crop_views('p/mobile', 390, height, [], cuts, self.config['slicing'])
            self.assertFalse(missing)
            self.assertEqual(crops[0]['core_bbox'][1], 0)
            self.assertEqual(crops[-1]['core_bbox'][3], height)
            if height <= 2700:
                self.assertEqual(cuts, [])
            else:
                self.assertTrue(trace['fallback'])
                for crop in crops:
                    self.assertGreaterEqual(crop['core_bbox'][3]-crop['core_bbox'][1], 900)
                    self.assertLessEqual(crop['crop_bbox'][3]-crop['crop_bbox'][1], 1800)
                self.assertTrue(all(a['core_bbox'][3] == b['core_bbox'][1] for a, b in zip(crops, crops[1:])))

    def test_crop_preserves_outside_parent_and_fragments(self):
        nodes, _, _ = normalize(source([node('p', [0, 0, 390, 8000], struct='block'),
                                        node('x', [0, 1300, 100, 300], 'p')]), 'page/mobile')
        before = deepcopy(nodes)
        crops, missing = crop_views('page/mobile', 390, 8000, nodes[1:], [1400, 2800, 4200, 5600, 6800], self.config['slicing'])
        positives = [n for c in crops for n in c['targets']]
        self.assertTrue(positives)
        self.assertFalse(missing)
        self.assertEqual(positives[0]['global_parent_id'], 'page/mobile/p')
        self.assertEqual(nodes, before)
        for crop in crops:
            offset = crop['local_to_global']['translation'][1]
            for target in crop['targets']:
                self.assertEqual(target['bbox'][1]+offset, 1300)

    def test_capacity_conflict_never_truncates_targets(self):
        nodes, _, _ = normalize(source([node(str(i), [0, 1, 40, 30]) for i in range(385)]), 'p/mobile')
        with self.assertRaisesRegex(ValueError, 'dense_capacity_conflict'):
            plan_cuts(390, 8000, nodes, [], self.config['slicing'])
        crops, _ = crop_views('p/mobile', 390, 900, nodes, [], self.config['slicing'])
        self.assertEqual(len(crops[0]['targets']), 385)
        self.assertEqual(crops[0]['status'], 'review')

    def test_medium_page_keeps_tall_target_and_large_background_checks_spatial_contents(self):
        nodes, w, h = normalize(source([node('image', [0, 0, 390, 2400], struct='image', subtype='image')], height=2500), 'p/mobile')
        result, _, _ = clean_nodes(nodes, w, h, self.config['cleanup'], 1800)
        self.assertTrue(result[0]['detection_eligible'])
        nodes, w, h = normalize(source([node('bg', [0, 0, 390, 7000], struct='image', subtype='background_image'),
                                        node('title', [0, 100, 200, 40], text_role='heading'),
                                        node('image', [0, 300, 100, 100], struct='image', subtype='image')]), 'p/mobile')
        result, _, _ = clean_nodes(nodes, w, h, self.config['cleanup'], 1800)
        self.assertFalse(result[0]['detection_eligible'])
        self.assertTrue(all(n['detection_eligible'] for n in result[1:]))

    def test_iou_counts_denominators_and_exact_one(self):
        result = iou_statistics([[0, 0, 10, 10], [0, 0, 10, 10], [20, 20, 30, 30]], self.config['statistics']['iou_edges'])
        self.assertEqual(result['pair_count'], 3)
        self.assertEqual(result['pair_bins']['zero'], 2)
        self.assertEqual(result['pair_bins']['[0.95,1.0]'], 1)
        self.assertEqual(result['max_iou_bins']['[0.95,1.0]'], 2)
        self.assertEqual(sum(result['max_iou_bins'].values()), 3)
        self.assertEqual(iou_statistics([[0, 0, 1, 1]], [0, 1])['singleton_boxes'], 1)

    def test_invalid_source_does_not_cancel_unrelated_viewports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, output = Path(tmp)/'input', Path(tmp)/'output'
            for viewport in ['good', 'bad']:
                folder = root/'pages'/'page'/viewport
                folder.mkdir(parents=True)
                raw = source([node('title', [10, 10, 150, 35], text_role='heading')], height=900)
                (folder/'hierarchy.json').write_text(json.dumps(raw) if viewport == 'good' else '[]')
                with Image.new('RGB', (390, 900), 'white') as image:
                    ImageDraw.Draw(image).rectangle([10, 10, 160, 45], fill='black')
                    image.save(folder/'full.png')
            summary = run_dataset(root, output, self.config)
            self.assertEqual(summary['statuses']['accepted'], 1)
            self.assertEqual(summary['statuses']['review'], 1)

    def test_local_end_to_end_isolation_resume_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, output = Path(tmp)/'input', Path(tmp)/'output'
            for viewport in ['desktop', 'mobile', 'tablet']:
                folder = root/'pages'/'page'/viewport
                folder.mkdir(parents=True)
                raw = source([node('title', [10, 10, 150, 35], text_role='heading')], height=900)
                (folder/'hierarchy.json').write_text(json.dumps(raw))
                if viewport == 'mobile':
                    (folder/'full.png').write_bytes(b'bad PNG')
                else:
                    with Image.new('RGB', (390, 900), 'white') as image:
                        ImageDraw.Draw(image).rectangle([10, 10, 160, 45], fill='black')
                        image.save(folder/'full.png')
            config = deepcopy(self.config)
            config.update(workers=2, pixel_workers=1)
            config['quality']['dense_min_targets'] = 3
            summary = run_dataset(root, output, config)
            self.assertEqual(summary['statuses']['accepted'], 2)
            self.assertEqual(summary['statuses']['review'], 1)
            before = (output/'crops'/'manifest.jsonl').read_bytes()
            summary = run_dataset(root, output, config, resume=True)
            self.assertEqual(summary['statuses']['resumed'], 2)
            self.assertEqual((output/'crops'/'manifest.jsonl').read_bytes(), before)
            stats = compute_statistics(output, workers=2)
            self.assertEqual(stats['totals']['published_unique_detection_boxes'], 2)
            self.assertEqual(stats['totals']['trainable_split_labels'], 2)
            self.assertEqual(stats['totals']['trainable_crops'], 2)
            # A damaged checkpoint must be recomputed, while unrelated observations resume.
            (output/'observations/page/desktop/result.json').write_text('{broken')
            summary = run_dataset(root, output, config, resume=True)
            self.assertEqual(summary['statuses']['accepted'], 2)
            self.assertEqual(summary['statuses']['resumed'], 1)
            # Changing one viewport can change page scope; cached siblings must be invalidated.
            raw = source([node('table', [0, 0, 390, 900], struct='table', subtype='table'),
                          *[node(str(i), [i*60, 100, 50, 16], 'table') for i in range(3)]], height=900)
            (root/'pages/page/desktop/hierarchy.json').write_text(json.dumps(raw))
            summary = run_dataset(root, output, config, resume=True)
            self.assertEqual(summary['statuses']['rejected'], 3)
            self.assertEqual(summary['statuses']['resumed'], 0)


if __name__ == '__main__':
    unittest.main()
