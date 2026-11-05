"""Four-level backfill and fifth archive regressions; no model service or screenshot mutation."""
from copy import deepcopy
import unittest
from test_postprocess import node,tree
from layer_accumulation import accumulate_to_fourth,size_groups,tiny_decision,refresh_archive_owners
from replacement_groups import replacement_plan,apply_replacement
from rules import validate


def fixture(fourth_struct='block'):
    return tree([node('r',box=[0,0,100,100]),node('p','r',[0,0,95,95]),
        node('q','p',[0,0,90,90]),{**node('fourth','q',[0,0,80,80]),'struct':fourth_struct}])


class AccumulationTest(unittest.TestCase):
    def test_missing_independent_objects_backfilled_without_extra_depth(self):
        original=fixture();original['nodes'] += [
            {**node('photo','fourth',[82,0,8,30]),'struct':'image'},
            {**node('title','fourth',[0,82,50,8]),'struct':'text','subtype':'heading'}]
        frozen=deepcopy(original)
        result,ops,summary=accumulate_to_fourth(original)
        self.assertEqual(original,frozen)
        self.assertEqual(summary['supplemented'],2)
        idx={n['node_id']:n for n in result['nodes']}
        for key in ('photo','title'):
            self.assertEqual(idx[key]['parent_id'],'q');self.assertEqual(idx[key]['layer'],3)
            self.assertEqual(idx[key]['bbox'],next(n['bbox'] for n in original['nodes'] if n['node_id']==key))
        validate(result,4)

    def test_atomic_l4_label_icon_and_contained_control_archived(self):
        original=fixture('control');original['nodes'] += [
            {**node('label','fourth',[5,5,50,20]),'struct':'text'},
            {**node('icon','fourth',[65,5,8,8]),'struct':'graphic'},
            {**node('action','fourth',[65,65,12,12]),'struct':'control'}]
        result,_,_=accumulate_to_fourth(original)
        idx={n['node_id']:n for n in result['nodes']};archive={n['node_id']:n for n in result['detail_archive']['nodes']}
        self.assertNotIn('action',idx)
        self.assertEqual(archive['action']['archive_reason'],'covered_by_existing_l4')
        self.assertEqual(archive['label']['archive_reason'],'already_represented_by_l4_object')
        self.assertEqual(archive['icon']['owner_id'],'fourth')

    def test_generic_containment_archives_instead_of_flattening_image(self):
        original=fixture();original['nodes'].append({**node('photo','fourth',[5,5,40,30]),'struct':'image'})
        result,_,_=accumulate_to_fourth(original)
        self.assertNotIn('photo',{n['node_id'] for n in result['nodes']})
        detail=next(n for n in result['detail_archive']['nodes'] if n['node_id']=='photo')
        self.assertEqual(detail['archive_reason'],'covered_by_existing_l4')
        self.assertEqual(detail['owner_id'],'fourth')
        self.assertEqual(detail['bbox'],[5,5,40,30])

    def test_existing_sibling_also_blocks_backfill(self):
        original=fixture();original['nodes'][-1]['bbox']=[0,0,10,10]
        original['nodes'] += [node('sibling','q',[20,20,60,60]),
            {**node('photo','fourth',[30,30,20,20]),'struct':'image'}]
        result,_,_=accumulate_to_fourth(original)
        detail=next(n for n in result['detail_archive']['nodes'] if n['node_id']=='photo')
        self.assertEqual(detail['owner_id'],'sibling')

    def test_missing_nested_frontier_does_not_flatten_all_descendants(self):
        original=fixture();original['nodes'][-1]['bbox']=[0,0,10,10]
        original['nodes'] += [node('region','fourth',[20,20,60,60]),
            {**node('photo','region',[25,25,40,30]),'struct':'image'},
            {**node('label','region',[25,60,40,10]),'struct':'text'}]
        result,_,summary=accumulate_to_fourth(original)
        self.assertEqual(summary['supplemented'],1)
        self.assertIn('region',{n['node_id'] for n in result['nodes']})
        self.assertEqual(summary['nested_supplements_archived'],2)
        shuffled=deepcopy(original);shuffled['nodes'].reverse()
        other,_,_=accumulate_to_fourth(shuffled)
        self.assertEqual({n['node_id'] for n in result['nodes']},{n['node_id'] for n in other['nodes']})

    def test_same_box_is_not_duplicated_and_l5_details_are_kept(self):
        original=fixture();original['nodes'].append({**node('photo','fourth',[0,0,80,80]),'struct':'image'})
        result,_,_=accumulate_to_fourth(original)
        self.assertEqual(sum(n['layer']==3 for n in result['nodes']),1)
        self.assertEqual(next(n for n in result['nodes'] if n['layer']==3)['node_id'],'photo')
        archive={n['node_id']:n for n in result['detail_archive']['nodes']}
        self.assertEqual(archive['fourth']['archive_reason'],'near_duplicate_visual_box')
        self.assertEqual(archive['fourth']['bbox'],[0,0,80,80])

    def test_single_figure_uses_more_precise_image_as_same_l4_object(self):
        original=fixture();original['nodes'][-1].update(subtype='figure',bbox=[0,5,80,70])
        original['nodes'].append({**node('photo','fourth',[0,0,80,80]),'struct':'image'})
        result,_,summary=accumulate_to_fourth(original)
        self.assertEqual(sum(n['layer']==3 for n in result['nodes']),1)
        self.assertEqual(summary['replaced_existing_l4'],1)
        self.assertEqual(summary['supplemented'],0)
        archive={n['node_id']:n for n in result['detail_archive']['nodes']}
        self.assertEqual(archive['fourth']['archive_reason'],'same_object_l4_representative')

    def test_archive_does_not_force_fake_main_layers_for_early_stopped_branch(self):
        original=fixture();original['nodes'].append({**node('detail','fourth',[5,5,4,4]),'struct':'graphic'})
        shallow=tree([{**node('r'),'struct':'image'}])
        result,_,_=accumulate_to_fourth(shallow,original)
        self.assertEqual(len(result['nodes']),1)
        self.assertEqual(validate(result)['r'],0)
        self.assertEqual(next(n for n in result['detail_archive']['nodes'] if n['node_id']=='detail')['owner_id'],'r')

    def test_width_and_height_protect_long_thin_boxes_even_when_crowded(self):
        capture={'width':1000,'height':8000};group={'group_id':'g','count':200,'penalty':4}
        for box in ([0,0,900,.01],[0,0,.01,900]):
            small,_=tiny_decision({'bbox':box},capture,group,{})
            self.assertFalse(small)

    def test_size_count_uses_both_dimensions_and_is_class_agnostic(self):
        nodes=[{**node(str(i),box=[i*31,0,30,20]),'struct':['text','image','control'][i%3]} for i in range(21)]
        nodes.append(node('different_shape',box=[0,100,60,10]))
        groups,lookup=size_groups(nodes)
        self.assertEqual(lookup['0']['count'],21)
        self.assertGreater(lookup['0']['penalty'],1)
        self.assertEqual(lookup['different_shape']['count'],1)
        # Duplicate DOM wrappers at identical coordinates count as one visual instance.
        nodes.append({**nodes[0],'node_id':'same_position'})
        self.assertEqual(size_groups(nodes)[1]['0']['count'],21)

    def test_frequency_penalty_starts_after_twenty_and_only_affects_small_boxes(self):
        n={'bbox':[0,0,20,20]};capture={'width':1000,'height':1000}
        _,lookup20=size_groups([node(str(i),box=[i*21,0,20,20]) for i in range(20)])
        _,lookup41=size_groups([node(str(i),box=[i*21,0,20,20]) for i in range(41)])
        self.assertFalse(tiny_decision(n,capture,lookup20['0'],{})[0])
        self.assertTrue(tiny_decision(n,capture,lookup41['0'],{})[0])

    def test_tiny_is_archived_and_previously_omitted_original_details_are_recovered(self):
        original=fixture();original['capture'].update(width=1000,height=1000)
        original['nodes'].append({**node('tiny','fourth',[85,85,1,1]),'struct':'graphic'})
        result,_,_=accumulate_to_fourth(original)
        self.assertNotIn('tiny',{n['node_id'] for n in result['nodes']})
        archive={n['node_id']:n for n in result['detail_archive']['nodes']}
        self.assertEqual(archive['tiny']['archive_reason'],'proportionally_tiny_detail')
        self.assertEqual(archive['tiny']['bbox'],[85,85,1,1])
        normalized=deepcopy(original);normalized['nodes']=normalized['nodes'][:-1]
        recovered,_,_=accumulate_to_fourth(normalized,original)
        self.assertIn('tiny',{n['node_id'] for n in recovered['detail_archive']['nodes']})
        self.assertTrue(recovered['detail_archive']['excluded_from_training_by_default'])

    def test_promote_existing_lower_box_replaces_upper_without_an_extra_wrapper(self):
        original=tree([node('upper',box=[0,0,95,95]),node('region','upper',[5,5,70,70]),
            {**node('a','upper',[10,10,20,20]),'struct':'image'},
            {**node('b','upper',[10,40,30,20]),'struct':'text'},node('rest','upper',[80,5,10,10])])
        policy={'prefer_existing_region_replacement':True,'accumulation_mode':True}
        plan,_=replacement_plan(original,['region','a','b'],policy)
        self.assertEqual(plan['action'],'promote_existing_lower_region')
        result,_=apply_replacement(original,['region','a','b'],'unused_group','visual_group',policy)
        idx={n['node_id']:n for n in result['nodes']}
        self.assertEqual(len(idx),len(original['nodes']))
        self.assertIsNone(idx['region']['parent_id'])
        self.assertEqual(idx['a']['parent_id'],'region')
        self.assertEqual(validate(result)['a'],validate(original)['a'])
        self.assertEqual(idx['upper']['bbox'],[80,5,10,10])

    def test_whole_upper_replaced_by_existing_lower_without_ignoring_foreground(self):
        original=tree([node('upper',box=[0,0,95,95]),node('region','upper',[5,5,70,70]),
            node('a','upper',[10,10,20,20]),node('b','upper',[10,40,30,20])])
        result,_=apply_replacement(original,['region','a','b'],'unused','visual_group',{'prefer_existing_region_replacement':True})
        self.assertNotIn('upper',{n['node_id'] for n in result['nodes']})
        self.assertFalse(result['ignore_regions'])
        self.assertIn('upper',result['postprocess_sources'])


if __name__=='__main__':unittest.main()
