"""Strict L5+ noise is removed before aggregation; primary content and source facts survive."""
import unittest
from copy import deepcopy
from test_postprocess import node
from test_semantic_regions import deep_tree
from semantic_cleanup import top_down_cleanup
from grouping import candidates


class DeepNoiseTest(unittest.TestCase):
    def test_original_deep_small_text_links_and_icons_removed(self):
        original=deep_tree();original['nodes'] += [
            {**node('fine','c3',[10,10,30,10]),'struct':'text','subtype':'text','text_role':'body'},
            {**node('link','c3',[10,25,40,12]),'struct':'control','subtype':'link'},
            {**node('icon','c3',[10,40,12,12]),'struct':'graphic','subtype':'svg'},
            {**node('heading','c3',[30,40,30,12]),'struct':'text','subtype':'heading'},
            {**node('photo','c3',[8,8,60,50]),'struct':'image'},
            {**node('input','c3',[30,25,20,10]),'struct':'control','subtype':'input_text'}]
        frozen=deepcopy(original)
        result,ops,summary=top_down_cleanup(original,{'deep_noise_mode':'strict'})
        removed={op['node'] for op in ops if op['op']=='drop'}
        self.assertEqual(removed,{'fine','link','icon'})
        self.assertTrue(all(op['original_level']==5 for op in ops if op['op']=='drop'))
        self.assertEqual(original,frozen)
        self.assertTrue({'heading','photo','input'} <= {n['node_id'] for n in result['nodes']})
        self.assertEqual(summary['strict_original_depth_noise']['selected'],3)
        proposed=candidates(result,{'group_action':'replace_upper','conservative_grouping':True})
        self.assertFalse(any(removed.intersection(g['members']) for g in proposed))

    def test_noise_only_container_removed_leaf_first(self):
        original=deep_tree();original['nodes'] += [node('wrap','c3',[10,10,45,45]),
            {**node('a','wrap',[10,10,30,10]),'struct':'text'},
            {**node('b','wrap',[10,30,30,10]),'struct':'control','subtype':'link'}]
        result,ops,_=top_down_cleanup(original,{'deep_noise_mode':'strict'})
        drops=[op['node'] for op in ops if op['op']=='drop']
        self.assertEqual(set(drops),{'wrap','a','b'});self.assertEqual(drops[-1],'wrap')
        self.assertTrue({'wrap','a','b'} <= set(result['postprocess_sources']))

    def test_original_depth_qualifies_noise_even_if_wrapper_could_promote_it(self):
        original=deep_tree();original['nodes'] += [node('wrapper','c3',[10,10,45,45]),
            {**node('tiny','wrapper',[10,10,30,10]),'struct':'text'}]
        result,ops,_=top_down_cleanup(original,{'deep_noise_mode':'strict'})
        self.assertNotIn('tiny',{n['node_id'] for n in result['nodes']})
        self.assertTrue(any(op['node']=='tiny' and op.get('original_level')==6 for op in ops))

    def test_tiny_deep_images_are_detail_but_large_media_survives(self):
        original=deep_tree();original['nodes'] += [
            {**node('badge','c3',[10,10,22,20]),'struct':'image'},
            {**node('photo','c3',[8,8,60,50]),'struct':'image'}]
        result,ops,_=top_down_cleanup(original,{'deep_noise_mode':'strict'})
        self.assertNotIn('badge',{n['node_id'] for n in result['nodes']})
        self.assertIn('photo',{n['node_id'] for n in result['nodes']})

    def test_shallow_small_link_and_large_body_preserved(self):
        original=deep_tree();original['nodes'] += [
            {**node('shallow','c2',[10,10,20,10]),'struct':'control','subtype':'link'},
            {**node('large','c3',[10,10,50,50]),'struct':'text','subtype':'text','text_role':'body'}]
        result,ops,_=top_down_cleanup(original,{'deep_noise_mode':'strict'})
        self.assertTrue({'shallow','large'} <= {n['node_id'] for n in result['nodes']})


if __name__=='__main__':unittest.main()
