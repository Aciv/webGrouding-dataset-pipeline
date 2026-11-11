"""Preserve fusion and cut evidence while publishing only real L1-L5 nodes and crop positives."""
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image, ImageDraw
from scripts.offline_postprocess.tests.test_offline import source, node
from scripts.offline_postprocess.infrastructure import sha256_file
from scripts.pipeline.layer_projection import PROMOTION_POLICY, truncate_graph, truncate_crop
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

    def test_l6_large_box_adsorbs_nearby_small_details_and_backfills_l5(self):
        def graph_box(key, parent, box, detection=True):
            return {'node_id':key, 'global_node_id':f'p/mobile/{key}', 'parent_id':parent,
                    'bbox':box, 'detection_eligible':detection, 'struct':'block',
                    'subtype':'visual_block', 'layer':99}
        nodes = [graph_box('n0', None, [0,0,1000,1000]),
                 graph_box('n1', 'n0', [0,0,1000,1000]),
                 graph_box('n2', 'n1', [0,0,1000,1000]),
                 graph_box('n3', 'n2', [0,0,1000,1000]),
                 graph_box('n4', 'n3', [0,0,50,50]),
                 graph_box('owner', 'n4', [100,100,400,400]),
                 graph_box('small1', 'owner', [410,100,450,140]),
                 graph_box('small2', 'owner', [120,120,160,160]),
                 graph_box('small3', 'small1', [420,110,445,135]),
                 graph_box('large_no_small', 'n4', [600,600,800,800]),
                 graph_box('small_alone', 'n4', [900,900,930,930])]
        graph, counts = truncate_graph({'nodes':nodes}, promotion=PROMOTION_POLICY)
        self.assertEqual([n['node_id'] for n in graph['nodes']], ['n0','n1','n2','n3','n4','owner'])
        promoted = graph['nodes'][-1]
        self.assertTrue(promoted['promoted'])
        self.assertEqual(promoted['parent_id'], 'n3')
        self.assertEqual((promoted['layer'], promoted['derived_depth']), (4,4))
        self.assertEqual(promoted['bbox'], [100,100,450,400])
        self.assertEqual(promoted['absorbed_node_ids'], ['p/mobile/small1','p/mobile/small2','p/mobile/small3'])
        self.assertEqual((counts['l6_promoted_owners'], counts['l6_absorbed_nodes']), (1,3))
        self.assertEqual(counts.get('l6_skipped_covered_by_l5', 0), 0)
        crop = {'crop_bbox':[0,0,1000,1000], 'core_bbox':[0,0,1000,1000],
                'targets':[{'global_node_id':n['global_node_id'], 'bbox':n['bbox']} for n in nodes],
                'ignore_regions':[]}
        final = truncate_crop(crop, graph)
        self.assertEqual([t['global_node_id'] for t in final['targets']],
                         ['p/mobile/n0','p/mobile/n1','p/mobile/n2','p/mobile/n3','p/mobile/n4','p/mobile/owner'])
        owner = final['targets'][-1]
        self.assertEqual(owner['bbox'], [100,100,450,400])
        self.assertEqual(owner['global_parent_id'], 'p/mobile/n3')
        self.assertEqual(owner['layer'], 4)
        self.assertEqual(owner['geometric_visibility'], 'complete')

        # An existing L5 target covering the same region suppresses the duplicate owner.
        covered = [dict(n) for n in nodes]
        covered[4] = graph_box('n4', 'n3', [90,90,470,410])
        graph, counts = truncate_graph({'nodes':covered}, promotion=PROMOTION_POLICY)
        self.assertNotIn('owner', [n['node_id'] for n in graph['nodes']])
        self.assertEqual(counts['l6_skipped_covered_by_l5'], 1)

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
