"""Exercise portable packing input and actual archive contents using small local fixtures."""
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from PIL import Image, ImageDraw
from scripts.offline_postprocess.tests.test_offline import node, source
from scripts.pipeline.components import run_component_pipeline
from scripts.pipeline.prepare import prepare_pack_input
from scripts.pipeline.layer_projection import project_final_layers
from scripts.pipeline.detail_selection import select_detail_crops
from scripts.pipeline.prepare_v6 import export_core_crops_v6
from scripts.pipeline.pack_v6 import pack_v6_export
from scripts.offline_postprocess.infrastructure import sha256_file
from scripts.hf_chunks.pack import pack_index


class PrepareTests(unittest.TestCase):
    def test_export_move_and_pack_only_accepted_viewport(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base/'input'
            for viewport in ('desktop', 'mobile'):
                folder = raw/'pages/page'/viewport
                folder.mkdir(parents=True)
                label = source([node('a', [10,20,100,30], text_role='heading')], width=200, height=900)
                label['capture'].update(url='https://example.test/', image='full.png')
                (folder/'hierarchy.json').write_text(json.dumps(label))
                with Image.new('RGB', (200,900), 'white') as image:
                    if viewport == 'mobile':
                        ImageDraw.Draw(image).rectangle((10,20,110,50), fill='black')
                    image.save(folder/'full.png')
            work = base/'work'
            run_component_pipeline(raw, work, workers=1, pixel_workers=1)
            final = work/'index-final.json'
            out = base/'prepared'
            result = prepare_pack_input(final, out, workers=1)
            self.assertEqual(result['observations'], 1)
            self.assertEqual(result['excluded_observations'], 1)
            self.assertEqual(result['missing_url'], 0)
            self.assertFalse((out/'pages/page/desktop').exists())
            exported = json.loads((out/'pages/page/mobile/hierarchy.json').read_text())
            self.assertEqual(exported['url'], 'https://example.test/')
            self.assertNotIn('pixel_diagnostics', exported['crops'][0])
            self.assertEqual(sha256_file(out/'pages/page/mobile/full.png'),
                             sha256_file(raw/'pages/page/mobile/full.png'))
            with self.assertRaises(ValueError):
                prepare_pack_input(final, out)
            moved = base/'moved'
            out.rename(moved)
            # Packaging must not read the original screenshots or intermediate labels.
            shutil.rmtree(raw)
            shutil.rmtree(work)
            package = base/'package'
            packed = pack_index(moved/'pack-index.json', package, require_url=True, dedupe_by='none')
            self.assertEqual(packed['images'], 1)
            self.assertEqual(packed['hierarchy_policy'], 'prepared_component_labels')
            metadata = json.loads((package/'metadata.jsonl').read_text().splitlines()[0])
            self.assertEqual(set(metadata['viewports']), {'mobile'})
            with tarfile.open(package/metadata['chunk']) as archive:
                item = metadata['viewports']['mobile']
                actual = json.load(archive.extractfile(item['hierarchy_member']))
                self.assertEqual(actual, exported)
                self.assertEqual(len(archive.getnames()), 2)

    def test_v6_export_materializes_core_height_crops_by_viewport(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base/'input'
            folder = raw/'pages/page/mobile'
            folder.mkdir(parents=True)
            label = source([node('a', [10,20,100,30], text_role='heading'),
                            node('b', [10,850,100,30], text_role='heading')], width=200, height=900)
            label['capture'].update(url='https://example.test/', image='full.png')
            (folder/'hierarchy.json').write_text(json.dumps(label))
            with Image.new('RGB', (200,900), 'white') as image:
                ImageDraw.Draw(image).rectangle((10,20,110,50), fill='black')
                ImageDraw.Draw(image).rectangle((10,850,110,880), fill='black')
                image.save(folder/'full.png')
            work = base/'work'
            run_component_pipeline(raw, work, workers=1, pixel_workers=1)
            projected = project_final_layers(work/'index-final.json', base/'layer', workers=1)
            selected = select_detail_crops(projected['final_index'], base/'detail', 0.2)
            result = export_core_crops_v6(selected['final_index'], base/'v6', workers=1)
            self.assertEqual(result['crops'], 1)
            self.assertTrue((base/'v6/pages/mobile/page_0000.png').is_file())
            crop = json.loads((base/'v6/pages/mobile/page_0000.json').read_text())
            with Image.open(base/'v6/pages/mobile/page_0000.png') as image:
                self.assertEqual(image.size, (200, 900))
            self.assertEqual(crop['image_size'], [200, 900])
            self.assertEqual(crop['coordinate_space'], 'core_crop_local_pixels')
            self.assertNotIn('crop_bbox', crop)
            self.assertFalse(crop['context_included'])
            self.assertTrue(crop['targets'])
            for target in crop['targets']:
                self.assertTrue(0 <= target['bbox'][0] < target['bbox'][2] <= 200)
                self.assertTrue(0 <= target['bbox'][1] < target['bbox'][3] <= 900)
            self.assertTrue((base/'v6/crops-index.json').is_file())
            packed = pack_v6_export(base/'v6', base/'v6pack', chunk_size_mb=1)
            self.assertEqual(packed['crops'], 1)
            row = json.loads((base/'v6pack/metadata.jsonl').read_text().strip())
            self.assertEqual(row['image_member'], 'pages/mobile/page_0000.png')
            with tarfile.open(base/'v6pack'/row['chunk']) as archive:
                names = archive.getnames()
                self.assertEqual(len(names), 2)
                self.assertIn(row['image_member'], names)
                self.assertIn(row['label_member'], names)


if __name__ == '__main__':
    unittest.main()
