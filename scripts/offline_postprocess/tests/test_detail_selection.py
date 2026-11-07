"""Ensure sparse crops can leave detail training without discarding their page or split task."""
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image, ImageDraw
from scripts.offline_postprocess.tests.test_offline import source, node
from scripts.offline_postprocess.infrastructure import read_jsonl, sha256_file
from scripts.pipeline.components import run_component_pipeline
from scripts.pipeline.detail_selection import target_union_coverage, select_detail_crops, target_box_pixel_evidence
from scripts.pipeline.prepare import prepare_pack_input
from scripts.hf_chunks.pack import pack_index


class DetailSelectionTests(unittest.TestCase):
    def test_pixel_boundary_original_coordinates_and_rgb(self):
        with Image.new('RGB',(100,40),(30,50,80)) as image:
            draw = ImageDraw.Draw(image)
            crop = {'crop_id':'c', 'crop_bbox':[0,20,100,40],
                    'targets':[{'global_node_id':str(i),'bbox':[i*20,0,(i+1)*20,20]} for i in range(5)]}
            for i in (3,4):
                draw.line((i*20,20,i*20+19,39),fill='white',width=2)
            evidence = target_box_pixel_evidence(image,crop)
            self.assertEqual(evidence['uninformative_boxes'],3)
            self.assertTrue(evidence['invalid'])
            draw.line((40,20,59,39),fill='white',width=2)
            self.assertFalse(target_box_pixel_evidence(image,crop)['invalid'])
            self.assertFalse(target_box_pixel_evidence(image,{**crop,'targets':[]})['checked'])
            # Per-channel RGB checks retain chromatic detail, even at similar brightness.
            draw.line((2,22,17,37),fill=(80,50,30),width=2)
            self.assertEqual(target_box_pixel_evidence(image,crop)['uninformative_boxes'],1)
            with self.assertRaises(ValueError):
                target_box_pixel_evidence(image,{**crop,'crop_bbox':[0,30,100,50]})

    def test_union_counts_overlap_once_and_threshold_equality_is_exact(self):
        crop = {'crop_bbox':[0,100,10,110], 'targets':[{'bbox':[0,0,4,5]}]*2,
                'ignore_regions':[{'bbox':[0,0,10,10]}]}
        self.assertEqual(target_union_coverage(crop),0.2)
        self.assertGreaterEqual(target_union_coverage(crop),0.2)
        with self.assertRaises(ValueError):
            target_union_coverage({**crop, 'crop_bbox':[0,0,0,10]})

    def test_siblings_split_labels_and_zero_detail_viewports_survive_export_and_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw, work = base/'input', base/'processing'
            folder = raw/'pages/page/mobile'
            folder.mkdir(parents=True)
            label = source([node('image1',[10,40,180,800],struct='image',subtype='image'),
                            node('small',[10,1800,80,20],text_role='heading'),
                            node('image2',[10,3000,180,800],struct='image',subtype='image')],
                           width=200,height=4000)
            label['capture']['url'] = 'https://example.test/'
            (folder/'hierarchy.json').write_text(json.dumps(label))
            with Image.new('RGB',(200,4000),'white') as image:
                draw = ImageDraw.Draw(image)
                for box in [(10,40,190,840),(10,1800,90,1820),(10,3000,190,3800)]:
                    draw.rectangle(box,fill='black')
                    draw.line((box[0],box[1],box[2]-1,box[3]-1),fill='white',width=3)
                image.save(folder/'full.png')
            config = base/'config.yaml'
            config.write_text('split_supervision:\n  include_fallback: true\n')
            run_component_pipeline(raw,work,config_path=config,workers=1,pixel_workers=1)
            source_index = work/'index-final.json'
            before = sha256_file(source_index)
            selected = base/'selected'
            report = select_detail_crops(source_index,selected,0.2)
            self.assertGreater(report['trainable_crops'],0)
            self.assertGreater(report['discarded_detail_crops'],0)
            final = json.loads((selected/'index-final.json').read_text())
            original = json.loads(source_index.read_text())
            self.assertEqual(final['observations'][0]['status'],'accepted')
            self.assertEqual(final['observations'][0]['split_supervision_eligible'],
                             original['observations'][0]['split_supervision_eligible'])
            self.assertEqual(final['artifacts']['split_labels'],original['artifacts']['split_labels'])
            self.assertEqual(before,sha256_file(source_index))
            self.assertTrue(all(c['detail_training_eligible'] for c in read_jsonl(selected/'crops/trainable.jsonl')))
            self.assertTrue(all(c['detail_training_discarded'] for c in read_jsonl(selected/'crops/discarded.jsonl')))
            self.assertTrue(select_detail_crops(source_index,selected,.2,resume=True)['resumed'])
            with self.assertRaises(ValueError):
                select_detail_crops(source_index,selected,.3,resume=True)
            export = base/'prepared'
            prepared = prepare_pack_input(selected/'index-final.json',export,workers=1)
            page = json.loads((export/'pages/page/mobile/hierarchy.json').read_text())
            self.assertEqual(len(page['detail_training_crop_ids']),report['trainable_crops'])
            self.assertEqual(len(page['discarded_detail_crop_ids']),report['discarded_detail_crops'])
            self.assertEqual(len(page['crops']),report['candidate_crops'])
            self.assertTrue(page['split_supervision_eligible'])
            packed = pack_index(export/'pack-index.json',base/'packed',require_url=True,dedupe_by='none')
            self.assertEqual(packed['images'],1)
            self.assertEqual(packed['detail_training_counts']['discarded_detail_crops'],report['discarded_detail_crops'])
            # 100% criterion drops all detail crops but never the viewport or full-page label.
            zero = base/'selected-zero'
            all_out = select_detail_crops(source_index,zero,1.0)
            self.assertEqual(all_out['trainable_crops'],0)
            self.assertEqual(all_out['accepted_observations'],1)
            empty_export = base/'prepared-zero'
            prepare_pack_input(zero/'index-final.json',empty_export,workers=1)
            page = json.loads((empty_export/'pages/page/mobile/hierarchy.json').read_text())
            self.assertFalse(page['detail_training_eligible'])
            self.assertTrue(page['split_supervision_eligible'])
            self.assertTrue(page['crops'])
            self.assertTrue(all(c['detail_training_discarded'] for c in page['crops']))
            zero_package = pack_index(empty_export/'pack-index.json',base/'packed-zero',require_url=True,dedupe_by='none')
            self.assertEqual(zero_package['images'],1)
            self.assertEqual(zero_package['detail_training_counts']['trainable_crops'],0)


if __name__ == '__main__':
    unittest.main()
