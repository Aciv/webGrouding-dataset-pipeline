"""Top-down cleanup, necessary grouping and DCGen-style pixel fallback; local fixtures only."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from PIL import Image, ImageDraw
from test_postprocess import node, tree
from rules import validate
from normalize import normalize
from semantic_cleanup import top_down_cleanup
from grouping import candidates, apply_decisions
from pixel_regions import separation_lines, pixel_partition, pixel_candidates, apply_pixel_decisions


def deep_tree():
    return tree([{**node('r',box=[0,0,95,95]),'struct':'region','subtype':'section'},
        {**node('c1','r',[2,2,85,85]),'subtype':'card'},
        {**node('c2','c1',[4,4,75,75]),'subtype':'card'},
        {**node('c3','c2',[6,6,65,65]),'subtype':'card'}])


def sections_image():
    img=Image.new('RGB',(300,300),'white');d=ImageDraw.Draw(img)
    d.rectangle((10,10,289,99),fill='black');d.rectangle((10,200,289,289),fill='#555555')
    return img


class SemanticRegionsTest(unittest.TestCase):
    def test_deep_primary_content_not_deleted_for_sparse_layer(self):
        original=deep_tree();original['nodes'].append({**node('img','c3',[10,10,40,40]),'struct':'image','subtype':'image'})
        result,ops,summary=top_down_cleanup(original)
        self.assertFalse(ops);self.assertIn('img',summary['meaningful_deep_retained'])
        result,ops,_=normalize(result,{'preserve_meaningful_deep':True,'layout_enabled':False})
        self.assertIn('img',{n['node_id'] for n in result['nodes']})
        self.assertFalse(any(op['op']=='drop' for op in ops))

    def test_deep_tiny_decorations_omitted_but_text_control_image_survive(self):
        original=deep_tree()
        original['nodes'] += [{**node('dot','c3',[10,10,3,3]),'struct':'graphic','subtype':'icon'},
            {**node('label','c3',[15,15,5,5]),'struct':'text','subtype':'text'},
            {**node('button','c3',[25,25,5,5]),'struct':'control','subtype':'button'},
            {**node('img','c3',[35,35,5,5]),'struct':'image','subtype':'image'}]
        result,ops,_=top_down_cleanup(original)
        self.assertEqual([op['node'] for op in ops if op['op']=='drop'],['dot'])
        self.assertTrue({'label','button','img'} <= {n['node_id'] for n in result['nodes']})
        self.assertTrue(any(x['node_id']=='dot' for x in result['ignore_regions']))

    def test_sparse_generic_wrapper_promotes_children(self):
        original=deep_tree();original['nodes'] += [node('wrap','c3',[8,8,55,55]),
            {**node('img','wrap',[10,10,40,40]),'struct':'image'}]
        result,ops,_=top_down_cleanup(original)
        self.assertTrue(any(op['op']=='contract' and op['node']=='wrap' for op in ops))
        self.assertIn('img',{n['node_id'] for n in result['nodes']})
        self.assertFalse(any(op['op']=='drop' for op in ops))

    def test_adequate_existing_parent_needs_no_group(self):
        original=tree([node('p',box=[0,0,95,60]),
            {**node('heading','p',[5,5,60,10]),'struct':'text','subtype':'heading','text_role':'heading'},
            {**node('body','p',[5,20,60,25]),'struct':'text','subtype':'text'}])
        self.assertEqual(candidates(original,{'conservative_grouping':True}),[])
        result,ops,rejected,_=apply_decisions(original,[{'candidate_id':'g1','members':['heading','body']}],
            {'groups':[{'candidate_id':'g1','decision':'accept'}]},semantic_policy={})
        self.assertFalse(ops);self.assertFalse(rejected);self.assertEqual(result,original)

    def test_pixel_partition_detects_whitespace_and_preserves_components(self):
        image=sections_image();policy={'pixel_window_px':12,'pixel_min_region_px':64,'pixel_max_depth':1}
        parts=pixel_partition(image,[0,0,300,300],[[10,10,280,90],[10,200,280,90]],policy)
        self.assertEqual(len(parts['zones']),2)
        self.assertTrue(parts['cuts'])
        blocked=pixel_partition(image,[0,0,300,300],[[10,10,280,280]],policy)
        self.assertEqual(len(blocked['zones']),1)
        self.assertGreater(blocked['blocked_cut_count'],0)

    def test_uniform_image_does_not_invent_partitions(self):
        self.assertEqual(len(pixel_partition(Image.new('RGB',(300,300),'white'),[0,0,300,300])['zones']),1)

    def test_pixel_approval_replaces_wrapper_without_an_extra_layer(self):
        original=tree([node('root',box=[0,0,300,300])]);original['capture'].update(width=300,height=300)
        frozen=deepcopy(original)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'full.png';sections_image().save(path)
            proposals,evidence=pixel_candidates(original,path,{'pixel_max_depth':1,'pixel_window_px':12})
        self.assertEqual(len(proposals),1)
        self.assertNotEqual(proposals[0]['parts'][0]['partition_bbox'],proposals[0]['parts'][0]['target_bbox'])
        refused,ops,errors=apply_pixel_decisions(original,proposals,[{'candidate_id':'p001','decision':'reject'}])
        self.assertEqual(refused,original);self.assertFalse(ops);self.assertFalse(errors)
        result,ops,errors=apply_pixel_decisions(original,proposals,[{'candidate_id':'p001','decision':'accept'}])
        self.assertFalse(errors);self.assertEqual(original,frozen)
        self.assertEqual(len(result['nodes']),2)
        self.assertTrue(all(n['parent_id'] is None and n['layer']==0 for n in result['nodes']))
        self.assertIn('root',result['postprocess_sources']);self.assertFalse(result['ignore_regions'])
        validate(result,4)

    def test_pixel_partition_preserves_recorded_members_and_bounds(self):
        original=tree([node('root',box=[0,0,300,300]),
            {**node('a','root',[10,10,280,90]),'struct':'text'},
            {**node('b','root',[10,200,280,90]),'struct':'text'}])
        original['capture'].update(width=300,height=300)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'full.png';sections_image().save(path)
            proposed,_=pixel_candidates(original,path,{'pixel_max_depth':1,'pixel_window_px':12})
        self.assertEqual(len(proposed),1)
        result,_,errors=apply_pixel_decisions(original,proposed,[{'candidate_id':'p001','decision':'accept'}])
        self.assertFalse(errors)
        index={n['node_id']:n for n in result['nodes']}
        for key in ('a','b'):
            self.assertEqual(index[key]['bbox'],next(n['bbox'] for n in original['nodes'] if n['node_id']==key))
            self.assertTrue(index[key]['parent_id'].startswith('pixel_region_'))
        validate(result,4)

    def test_shallow_tiny_decoration_not_removed(self):
        original=tree([{**node('icon',box=[1,1,3,3]),'struct':'graphic','subtype':'icon'}])
        result,ops,_=top_down_cleanup(original)
        self.assertFalse(ops);self.assertEqual(result['nodes'][0]['bbox'],[1,1,3,3])

    def test_pixel_model_cannot_edit_coordinates(self):
        _,_,rejected=apply_pixel_decisions(tree([]),[],[{'candidate_id':'p001','decision':'accept','bbox':[0,0,10,10]}])
        self.assertTrue(rejected)

    def test_existing_atomic_image_never_pixel_split(self):
        original=tree([{**node('root',box=[0,0,300,300]),'struct':'image'}]);original['capture'].update(width=300,height=300)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'full.png';sections_image().save(path)
            proposed,evidence=pixel_candidates(original,path)
        self.assertFalse(proposed);self.assertFalse(evidence)


if __name__=='__main__':unittest.main()
