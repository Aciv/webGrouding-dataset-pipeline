"""Deterministic regression cases for two-stage filtering and index publication."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from PIL import Image, ImageDraw
from scripts.page_filter.stages import color_evidence, pre_filter_source, post_filter_crops
from scripts.offline_postprocess.config import load_config
from scripts.offline_postprocess.infrastructure import sha256_file
from scripts.offline_postprocess.tests.test_offline import node, source
from scripts.pipeline.components import run_component_pipeline


class FilterPipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_white_black_color_and_tiny_specks_are_rejected(self):
        for color in [(255,255,255), (0,0,0), (40,160,210)]:
            image = np.empty((900,200,3), dtype=np.uint8)
            image[:] = color
            self.assertTrue(color_evidence(image, self.config['blank_crop_filter'])['rejected'])
        image[:] = 255
        image[400,100] = 0
        self.assertTrue(color_evidence(image, self.config['blank_crop_filter'])['rejected'])

    def test_white_background_with_content_and_colored_low_luminance_variance_are_kept(self):
        image = np.full((900,200,3),255,dtype=np.uint8)
        image[50:70,30:170] = 0
        self.assertFalse(color_evidence(image,self.config['blank_crop_filter'])['rejected'])
        image[::2] = (255,0,0)
        image[1::2] = (0,130,0)
        self.assertFalse(color_evidence(image,self.config['blank_crop_filter'])['rejected'])

    def test_core_blank_is_not_hidden_by_context_content(self):
        image = np.full((1200,200,3),255,dtype=np.uint8)
        image[:50] = 0
        crops = [{'crop_id':'page/mobile/0', 'crop_bbox':[0,0,200,1200], 'core_bbox':[0,100,200,1100]}]
        result=post_filter_crops(image,200,crops,self.config['blank_crop_filter'])
        self.assertTrue(result['rejected'])
        self.assertEqual(result['crops'][0]['trigger_scopes'],['core'])

    def test_minimum_box_count_is_not_responsiveness_proof(self):
        single = source([node('title',[10,10,150,30],text_role='heading')],height=900)
        evidence=pre_filter_source(single,self.config['pre_filter'])
        self.assertEqual(evidence['status'],'accepted')
        self.assertEqual(evidence['responsive_status'],'unknown')
        self.assertEqual(evidence['rules']['min_boxes'],0)
        single['nodes'][0]['layout_bbox']=[0,10,1000,30]
        evidence=pre_filter_source(single,self.config['pre_filter'])
        self.assertEqual(evidence['status'],'accepted')
        self.assertEqual(evidence['geometry']['layout_horizontal_outside'],1)
        self.assertEqual(evidence['geometry']['layout_warnings'],0)

    def test_restored_layout_gate_uses_historical_count_ratio_and_width_thresholds(self):
        nodes = [node(str(i),[10,20+i*25,100,20],text_role='heading') for i in range(25)]
        raw = source(nodes,height=900,width=200)
        for n in raw['nodes'][:5]:
            n['layout_bbox'] = [20,20,200,20]  # Exactly 10% lost width.
        evidence = pre_filter_source(raw,self.config['pre_filter'])
        self.assertEqual(evidence['status'],'rejected')
        self.assertEqual(evidence['reason_codes'],['excessive_out_of_bounds'])
        self.assertEqual(evidence['geometry']['outside_ratio'],0.2)
        self.assertEqual(evidence['geometry']['training_outside_boxes'],0)
        raw['nodes'][4]['layout_bbox'] = [19,20,200,20]
        self.assertEqual(pre_filter_source(raw,self.config['pre_filter'])['status'],'accepted')
        raw['nodes'][4]['layout_bbox'] = [20,20,200,20]
        warned = pre_filter_source(raw,{**self.config['pre_filter'],'layout_overflow_policy':'warn'})
        self.assertEqual(warned['status'],'accepted')
        self.assertEqual(warned['geometry']['layout_warnings'],5)

    def test_layout_rejection_does_not_remove_normal_sibling_or_sparse_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, output = Path(temporary)/'input', Path(temporary)/'output'
            for viewport, width in [('desktop',1200),('mobile',200)]:
                folder = root/'pages/page'/viewport
                folder.mkdir(parents=True)
                raw = source([node(str(i),[10,30+i*80,100,30],text_role='heading') for i in range(6)],
                             width=width,height=900)
                raw['capture']['image'] = 'full.png'
                for n in raw['nodes']:
                    n['layout_bbox'] = [10,30,900,30]
                (folder/'hierarchy.json').write_text(json.dumps(raw))
                with Image.new('RGB',(width,900),'white') as image:
                    draw = ImageDraw.Draw(image)
                    for i in range(6): draw.rectangle((10,30+i*80,110,60+i*80),fill='black')
                    image.save(folder/'full.png')
            run_component_pipeline(root,output,workers=1,pixel_workers=1)
            index = json.loads((output/'index-final.json').read_text())
            observations = {o['viewport']:o for o in index['observations']}
            self.assertEqual(observations['desktop']['status'],'accepted')
            self.assertEqual(observations['mobile']['status'],'rejected')
            self.assertEqual(observations['mobile']['published_crops'],0)
            self.assertFalse(observations['mobile']['split_supervision_eligible'])
            bad = json.loads((output/'observations/page/mobile/result.json').read_text())
            self.assertEqual(bad['crops'],[])
            self.assertEqual(bad['operations'],[])
            with (output/'crops/trainable.jsonl').open() as stream:
                self.assertTrue(all(json.loads(line)['observation_id']=='page/desktop' for line in stream))

    def test_any_blank_crop_rejects_viewport_and_pipeline_publishes_no_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root,output=Path(temporary)/'input',Path(temporary)/'output'
            for viewport in ['desktop','mobile']:
                directory=root/'pages/page'/viewport
                directory.mkdir(parents=True)
                raw=source([node('a',[10,30,150,35],text_role='heading'),
                            node('ghost',[10,1700,150,35],text_role='heading'),
                            node('z',[10,3200,150,35],text_role='heading')],height=4000,width=200)
                (directory/'hierarchy.json').write_text(json.dumps(raw))
                with Image.new('RGB',(200,4000),'white') as image:
                    draw=ImageDraw.Draw(image)
                    draw.rectangle([10,30,160,65],fill='black')
                    draw.rectangle([10,3200,160,3235],fill='black')
                    if viewport=='mobile':
                        for y in range(100,3900,120):draw.rectangle([10,y,160,y+20],fill='black')
                    image.save(directory/'full.png')
            before=sha256_file(root/'pages/page/desktop/full.png')
            summary=run_component_pipeline(root,output,workers=2,pixel_workers=1)
            self.assertTrue(summary['validation_passed'])
            rejected=json.loads((output/'observations/page/desktop/result.json').read_text())
            self.assertEqual(rejected['pre_filter']['status'],'accepted')
            self.assertEqual(rejected['quality']['status'],'rejected')
            self.assertIn('blank_or_solid_crop',rejected['quality']['reason_codes'])
            self.assertFalse(rejected['split_label']['supervision_eligible'])
            self.assertTrue(all(c['status']=='rejected' for c in rejected['crops']))
            self.assertTrue(rejected['quality']['evidence']['post_filter']['failed_crop_ids'])
            index=json.loads((output/'index-final.json').read_text())
            records={r['observation_id']:r for r in index['observations']}
            self.assertEqual(records['page/desktop']['published_crops'],0)
            self.assertEqual(records['page/mobile']['status'],'accepted')
            self.assertTrue(all(json.loads(line)['observation_id']=='page/mobile'
                                for line in (output/'crops/trainable.jsonl').read_text().splitlines()))
            self.assertEqual(sha256_file(root/'pages/page/desktop/full.png'),before)
            self.assertTrue((output/'index-filtered.json').exists())
            self.assertTrue((output/'index-postprocessed.json').exists())


if __name__=='__main__':
    unittest.main()
