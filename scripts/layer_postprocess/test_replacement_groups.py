"""Upper-region replacement regressions: no deeper existing targets, no silent loss."""
from copy import deepcopy
import unittest
from test_postprocess import node,tree
from rules import validate
from grouping import apply_decisions,candidates
from replacement_groups import replacement_plan,apply_replacement


def sample():
    return tree([node('p',box=[0,0,95,95]),
        {**node('a','p',[5,5,35,15]),'struct':'text','subtype':'heading','text_role':'heading'},
        {**node('b','p',[5,25,35,20]),'struct':'text'},
        {**node('c','p',[65,5,15,15]),'struct':'image'}])


class ReplacementGroupsTest(unittest.TestCase):
    def test_split_upper_preserves_existing_depth_and_atomic_boxes(self):
        original=sample();frozen=deepcopy(original)
        result,ops=apply_replacement(original,['a','b'],'vlm_group_test','heading_content')
        before=validate(original);after=validate(result);idx={n['node_id']:n for n in result['nodes']}
        self.assertEqual(original,frozen)
        for k in before:self.assertEqual(after[k],before[k])
        self.assertEqual(after['vlm_group_test'],before['p'])
        self.assertIsNone(idx['vlm_group_test']['parent_id'])
        self.assertEqual(idx['p']['bbox'],[65,5,15,15])
        for n in original['nodes'][1:]:self.assertEqual(idx[n['node_id']]['bbox'],n['bbox'])
        self.assertEqual(result['postprocess_original_geometry']['p']['bbox'],[0,0,95,95])
        self.assertFalse(result['ignore_regions'])

    def test_existing_component_and_virtual_root_not_partitioned(self):
        original=sample();original['nodes'][0]['subtype']='card'
        self.assertIsNone(replacement_plan(original,['a','b'])[0])
        original=sample();original['nodes']=original['nodes'][1:]
        for n in original['nodes']:n['parent_id']=None
        self.assertIsNone(replacement_plan(original,['a','b'])[0])

    def test_whole_family_reuses_parent(self):
        self.assertIsNone(replacement_plan(sample(),['a','b','c'])[0])

    def test_model_executes_replacement_not_wrapper_insertion(self):
        original=sample();proposal={'candidate_id':'g1','members':['a','b']}
        result,ops,rejected,_=apply_decisions(original,[proposal],{'groups':[{'candidate_id':'g1','decision':'accept','region_type':'heading_content'}]},semantic_policy={'group_action':'replace_upper'})
        self.assertFalse(rejected)
        self.assertEqual(validate(result)['a'],validate(original)['a'])
        self.assertTrue(any(op['op']=='resize_upper_region' for op in ops))

    def test_replacement_can_include_deep_targets_without_deepening(self):
        original=sample()
        original['nodes'] += [node('x','b',[5,25,25,15]),node('y','x',[5,25,20,10]),{**node('leaf','y',[5,25,15,5]),'struct':'image'}]
        proposals=candidates(original,{'conservative_grouping':True,'group_action':'replace_upper'})
        self.assertTrue(any(set(g['members'])=={'a','b'} for g in proposals))
        result,_=apply_replacement(original,['a','b'],'vlm_group_test','heading_content')
        self.assertEqual(validate(result)['leaf'],validate(original)['leaf'])


if __name__=='__main__':unittest.main()
