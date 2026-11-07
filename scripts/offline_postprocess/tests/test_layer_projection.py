"""Preserve fusion and cut evidence while publishing only real L1-L5 nodes and crop positives."""
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image, ImageDraw
from scripts.offline_postprocess.tests.test_offline import source, node
from scripts.offline_postprocess.infrastructure import sha256_file
from scripts.pipeline.layer_projection import truncate_graph, truncate_crop
from scripts.pipeline.components import run_component_pipeline
from scripts.pipeline.prepare import prepare_pack_input


class LayerProjectionTests(unittest.TestCase):
    def test_truncation_keeps_fused_nodes_and_does_not_promote_deep_children(self):
        nodes = [{'node_id':str(i),'global_node_id':f'p/mobile/{i}','parent_id':str(i-1) if i else None,
                  'derived_depth':i,'layer':99,'bbox':[0,0,100,100],'detection_eligible':i!=3} for i in range(7)]
        nodes[2].update(generated=True,subtype='content_group',source_node_ids=['a','b'])
        nodes[3]['represented_by']='p/mobile/2'
        graph,counts = truncate_graph({'nodes':nodes})
        self.assertEqual([n['node_id'] for n in graph['nodes']],list('01234'))
        self.assertEqual([n['layer'] for n in graph['nodes']],list(range(5)))
        self.assertTrue(graph['nodes'][2]['generated'])
        self.assertFalse(graph['nodes'][3]['detection_eligible'])
        self.assertEqual(graph['nodes'][3]['represented_by'],'p/mobile/2')
        self.assertEqual(counts['nodes_before']-counts['nodes_after'],2)
        crop={'crop_bbox':[0,0,100,100],'core_bbox':[0,0,100,100],
              'targets':[{'global_node_id':n['global_node_id'],'global_parent_id':f"p/mobile/{n['parent_id']}" if n['parent_id'] else None,'bbox':n['bbox']} for n in nodes if n['detection_eligible']],
              'ignore_regions':[{'global_node_id':'p/mobile/6','bbox':[0,0,10,10]}, {'bbox':[10,10,20,20],'reason':'unknown_source_ignore'}]}
        final = truncate_crop(crop,graph)
        self.assertEqual([t['global_node_id'] for t in final['targets']],['p/mobile/0','p/mobile/1','p/mobile/2','p/mobile/4'])
        self.assertEqual(final['crop_bbox'],crop['crop_bbox'])
        self.assertEqual(len(final['ignore_regions']),1)
        with self.assertRaises(ValueError):truncate_graph({'nodes':nodes},6)

    def test_standalone_export_cannot_bypass_depth_limit_or_reuse_old_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);raw=base/'input';folder=raw/'pages/page/mobile';folder.mkdir(parents=True)
            nodes=[node(str(i),[0,0,200,700],str(i-1) if i else None,struct='block',layer=i,
                        disposition='structure_only') for i in range(6)]
            nodes.append(node('6',[40,400,120,80],'5',struct='image',subtype='image',layer=6))
            label=source(nodes,width=200,height=900);label['capture']['url']='https://example.test/'
            (folder/'hierarchy.json').write_text(json.dumps(label))
            with Image.new('RGB',(200,900),'white') as image:
                ImageDraw.Draw(image).rectangle((40,400,160,480),fill='black');image.save(folder/'full.png')
            work=base/'processing';run_component_pipeline(raw,work,workers=1,pixel_workers=1)
            original=json.loads((work/'index-final.json').read_text());self.assertEqual(original['observations'][0]['status'],'accepted')
            source_hash=sha256_file(work/'index-final.json');output=base/'prepared'
            prepare_pack_input(work/'index-final.json',output,workers=1)
            page=json.loads((output/'pages/page/mobile/hierarchy.json').read_text())
            self.assertEqual(len(page['nodes']),5)
            self.assertTrue(all(n['layer']<=4 and n['derived_depth']<=4 for n in page['nodes']))
            self.assertEqual(page['detection_target_ids'],[])
            self.assertEqual(page['crops'][0]['targets'],[])
            self.assertEqual(page['crops'][0]['target_union_coverage'],0)
            self.assertFalse(page['crops'][0]['detail_training_eligible'])
            self.assertTrue(page['split_supervision_eligible'])
            self.assertEqual(source_hash,sha256_file(work/'index-final.json'))
