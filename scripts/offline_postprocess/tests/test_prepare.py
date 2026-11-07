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


if __name__ == '__main__':
    unittest.main()
