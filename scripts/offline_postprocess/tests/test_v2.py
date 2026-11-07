"""Local deterministic tests for bounded noise, local region evidence and content coverage."""
from copy import deepcopy
import unittest
import tempfile
import json
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from scripts.offline_postprocess.tests.test_offline import node, source
from scripts.offline_postprocess.config import load_config
from scripts.offline_postprocess.domain import normalize, graph_depths
from scripts.offline_postprocess.cleanup import clean_nodes
from scripts.offline_postprocess.regions import propose_regions
from scripts.offline_postprocess.slicing import supervision_qualification, plan_views
from scripts.offline_postprocess.slicing_statistics import rectangle_union_area
from scripts.offline_postprocess.application import run_dataset
from scripts.offline_postprocess.infrastructure import sha256_file


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_single_inline_link_activates_existing_paragraph(self):
        raw = source([node('p', [20, 100, 300, 180], struct='block', subtype='content_group',
                           tag='p', disposition='structure_only'),
                      node('link', [40, 240, 80, 16], 'p', struct='control', subtype='link')])
        normalized, w, h = normalize(raw, 'p/mobile')
        nodes, _, _ = clean_nodes(normalized, w, h, self.config['cleanup'], 1800)
        p, link = nodes[:2]
        self.assertTrue(p['detection_eligible'])
        self.assertEqual(p['content_representative_basis'], 'source_paragraph_and_inline_content')
        self.assertFalse(link['detection_eligible'])
        self.assertEqual(link['represented_by'], p['global_node_id'])
        self.assertEqual(raw['nodes'][0]['disposition'], 'structure_only')

    def test_local_pixels_create_only_source_supported_regions(self):
        raw = source([node('root', [0, 0, 390, 4000], struct='block', subtype='visual_block'),
                      *[node(str(i), [30, y, 300, 80], 'root', text_role='heading')
                        for i, y in enumerate([100, 900, 2200, 3100])]], height=4000)
        nodes, w, h = normalize(raw, 'p/mobile')
        gray = np.full((4000, 390), 255, dtype=np.uint8)
        for y in [100, 900, 2200, 3100]:
            gray[y:y+80, 30:330] = 0
        proposals = propose_regions(nodes, w, h, gray, self.config['local_regions'], self.config['pixels'], 1800)
        self.assertTrue(proposals)
        cleaned, operations, _ = clean_nodes(nodes, w, h, self.config['cleanup'], 1800, 2700, proposals)
        self.assertTrue(any(o['op'] == 'apply_local_partition' for o in operations))
        self.assertFalse(cleaned[0]['detection_eligible'])
        generated = [n for n in cleaned if n.get('subtype') == 'pixel_content_region']
        self.assertGreaterEqual(len(generated), 2)
        self.assertTrue(all(n['source_node_ids'] and n['bbox'][3]-n['bbox'][1] <= 1800 for n in generated))
        self.assertTrue(all(n['detection_eligible'] for n in cleaned[1:5]))
        graph_depths(cleaned)
        empty = propose_regions(nodes[:1], w, h, gray, self.config['local_regions'], self.config['pixels'], 1800)
        self.assertEqual(empty, [])

    def test_component_boundary_loss_is_tolerated_but_content_loss_is_not(self):
        nodes, _, _ = normalize(source([node('box', [0, 0, 300, 580], struct='block', subtype='listitem'),
                                        *[node(str(i), [0, 1000+i*50, 200, 30], text_role='heading') for i in range(10)]]), 'p/mobile')
        p = {'fallback': False}
        crops = [{'reason_codes': [], 'status': 'accepted'}]
        grade = supervision_qualification(nodes, crops, [nodes[0]['global_node_id']], p, self.config['split_supervision'])
        self.assertTrue(grade['eligible'])
        self.assertEqual(grade['quality_tier'], 'tolerated_component_boundary')
        grade = supervision_qualification(nodes, crops, [nodes[1]['global_node_id']], p, self.config['split_supervision'])
        self.assertFalse(grade['eligible'])
        self.assertEqual(grade['reason_codes'], ['critical_target_without_complete_view'])

    def test_critical_recovery_preserves_foreground_without_mutating_graph(self):
        nodes, _, _ = normalize(source([node('photo', [40, 400, 300, 1700], struct='image', subtype='image')], height=3184), 'p/mobile')
        original = deepcopy(nodes)
        cuts, partition, crops, missing, grade = plan_views('p/mobile', 390, 3184, nodes, [],
            self.config['slicing'], self.config['split_supervision'])
        self.assertFalse(grade['critical_missing_ids'])
        self.assertFalse(missing)
        self.assertTrue(any(c['targets'] for c in crops))
        self.assertEqual(nodes, original)
        self.assertTrue(all(900 <= c['crop_bbox'][3]-c['crop_bbox'][1] <= 1800 for c in crops))

    def test_ignore_union_does_not_double_count_nested_boxes(self):
        self.assertEqual(rectangle_union_area([[0, 0, 10, 10], [2, 2, 8, 8]]), 100)
        self.assertEqual(rectangle_union_area([[0, 0, 10, 10], [5, 0, 15, 10]]), 150)

    def test_confirmed_error_uses_image_fingerprint_and_peers_remain_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, output = Path(temporary)/'source', Path(temporary)/'output'
            for viewport in ['desktop', 'mobile']:
                folder = root/'pages/page'/viewport
                folder.mkdir(parents=True)
                (folder/'hierarchy.json').write_text(json.dumps(source([
                    node('title', [10, 10, 150, 35], text_role='heading')], height=900)))
                with Image.new('RGB', (390, 900), 'white') as image:
                    ImageDraw.Draw(image).rectangle([10, 10, 160, 45], fill='black')
                    image.save(folder/'full.png')
            config = deepcopy(self.config)
            config['quality']['confirmed_error_observations'] = {'page/desktop': {
                'reason': 'confirmed_service_error', 'image_sha256': sha256_file(root/'pages/page/desktop/full.png')}}
            summary = run_dataset(root, output, config)
            self.assertEqual(summary['statuses']['rejected'], 1)
            self.assertEqual(summary['statuses']['review'], 1)
            with Image.new('RGB', (390, 900), 'gray') as changed:
                ImageDraw.Draw(changed).rectangle([10, 10, 160, 45], fill='black')
                changed.save(root/'pages/page/desktop/full.png')
            summary = run_dataset(root, output, config, resume=True)
            self.assertEqual(summary['statuses']['review'], 2)
            result = json.loads((output/'observations/page/desktop/result.json').read_text())
            self.assertIn('confirmed_error_source_changed', result['quality']['reason_codes'])


if __name__ == '__main__':
    unittest.main()
