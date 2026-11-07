"""Verify real stop boundaries, resume hashes and compression through the public CLI parser."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image, ImageDraw
from scripts.offline_postprocess.tests.test_offline import source, node
from scripts.pipeline.cli import build_parser


class StagedPipelineTests(unittest.TestCase):
    def test_each_stop_and_resume_to_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw, work = base/'input', base/'processing'
            for viewport in ('desktop', 'mobile'):
                folder = raw/'pages/page'/viewport
                folder.mkdir(parents=True)
                label = source([node('a',[10,30,150,35],text_role='heading'),
                                node('ghost',[10,1700,150,35],text_role='heading'),
                                node('z',[10,3200,150,35],text_role='heading')],height=4000,width=200)
                label['capture'].update(url='https://example.test/', image='full.png')
                (folder/'hierarchy.json').write_text(json.dumps(label))
                with Image.new('RGB',(200,4000),'white') as image:
                    draw = ImageDraw.Draw(image)
                    draw.rectangle([10,30,160,65],fill='black')
                    draw.rectangle([10,3200,160,3235],fill='black')
                    if viewport == 'mobile':
                        for y in range(100,3900,120):
                            draw.rectangle([10,y,160,y+20],fill='black')
                    image.save(folder/'full.png')
            def run(stage, resume=True):
                argv = ['run','--input',str(raw),'--workdir',str(work),
                        '--workers','1','--pixel-workers','1','--pack-workers','1',
                        '--stop-after',stage]
                if resume:
                    argv.append('--resume')
                args = build_parser().parse_args(argv)
                with contextlib.redirect_stdout(io.StringIO()):
                    args.func(args)
            run('filter', False)
            self.assertTrue((work/'index-filtered.json').is_file())
            self.assertFalse((work/'index-postprocessed.json').exists())
            result = work/'observations/page/desktop/result.json'
            filtered = json.loads(result.read_text())
            self.assertEqual(filtered['completed_stage'], 'filter')
            self.assertEqual(filtered['operations'], [])
            self.assertEqual(filtered['crops'], [])
            stamp = result.stat().st_mtime_ns
            run('filter')
            self.assertEqual(stamp, result.stat().st_mtime_ns)
            run('postprocess')
            self.assertTrue((work/'index-postprocessed.json').is_file())
            self.assertFalse((work/'index-final.json').exists())
            candidate = json.loads(result.read_text())
            self.assertEqual(candidate['completed_stage'], 'postprocess')
            self.assertTrue(candidate['crops'])
            self.assertFalse(candidate['quality']['evidence']['post_filter']['checked'])
            self.assertEqual((work/'crops/trainable.jsonl').read_text(), '')
            self.assertEqual((work/'split_labels/trainable.jsonl').read_text(), '')
            run('final-filter')
            final = work/'index-final.json'
            self.assertTrue(final.is_file())
            self.assertEqual(json.loads(result.read_text())['quality']['status'],'rejected')
            prepared = work.with_name(work.name+'-pack-input')
            chunks = work.with_name(work.name+'-chunks')
            self.assertFalse(prepared.exists())
            run('layer-filter')
            self.assertTrue(work.with_name(work.name+'-final-l5').joinpath('index-final.json').is_file())
            self.assertFalse(prepared.exists())
            run('detail-filter')
            self.assertTrue(work.with_name(work.name+'-detail-selection').joinpath('index-final.json').is_file())
            self.assertFalse(prepared.exists())
            run('prepare-pack')
            self.assertTrue((prepared/'pack-index.json').is_file())
            self.assertFalse(chunks.exists())
            run('pack')
            manifest = json.loads((chunks/'manifest.json').read_text())
            self.assertEqual(manifest['images'], 1)
            self.assertEqual(manifest['missing_url'], 0)
            files = [final, prepared/'pack-index.json', chunks/'manifest.json',
                     chunks/manifest['shards'][0]['file']]
            stamps = [file.stat().st_mtime_ns for file in files]
            run('pack')
            self.assertEqual(stamps, [file.stat().st_mtime_ns for file in files])
            mismatch = build_parser().parse_args(['run','--input',str(raw),'--workdir',str(work),
                '--workers','2','--pixel-workers','1','--stop-after','pack','--resume'])
            with self.assertRaises(ValueError), contextlib.redirect_stdout(io.StringIO()):
                mismatch.func(mismatch)
            self.assertEqual(stamps, [file.stat().st_mtime_ns for file in files])
            # A source change must recompute and select fresh immutable downstream outputs.
            changed = raw/'pages/page/mobile/hierarchy.json'
            label = json.loads(changed.read_text())
            label['capture']['documentId'] = 'changed-source'
            changed.write_text(json.dumps(label))
            run('pack')
            self.assertTrue(prepared.with_name(prepared.name+'-2').is_dir())
            self.assertTrue(chunks.with_name(chunks.name+'-2').is_dir())


if __name__ == '__main__':
    unittest.main()
