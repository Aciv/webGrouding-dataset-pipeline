"""Aggregate lower blocks by partitioning their existing upper wrapper, without adding depth."""
from copy import deepcopy
from rules import apply_operations, union, validate
from semantic_cleanup import generic_wrapper, necessary_group


def replacement_plan(tree, ids, policy=None):
    """Describe a fixed parent-region split; virtual roots and already coherent components stay intact."""
    policy = policy or {}
    index = {n['node_id']: n for n in tree['nodes']}
    members = [index[k] for k in ids]
    if len(members)<2 or len({n['parent_id'] for n in members}) != 1:
        return None, 'members_need_one_existing_parent'
    parent_id = members[0]['parent_id']
    if parent_id is None:
        return None, 'no_upper_wrapper_to_replace'
    parent = index[parent_id]
    # Existing cards, navigation, footer, forms, lists and tables are not split just to
    # introduce a more convenient grouping. Their existing grouping may be sufficient.
    eligible = generic_wrapper(parent) or parent.get('subtype') in {'section','article','main','group','region'}
    if policy.get('accumulation_mode') and parent.get('struct') == 'list':
        eligible = True
    if not eligible or parent.get('struct') not in {'block','region','list'}:
        return None, 'preserve_existing_component_parent'
    if parent['bbox'][2]*parent['bbox'][3] < policy.get('replacement_min_parent_area_px2',4096):
        return None, 'upper_wrapper_too_small_for_partition'
    bbox=union([n['bbox'] for n in members])
    if policy.get('prefer_existing_region_replacement'):
        representatives=[n for n in members if n.get('struct') in {'block','region','list','table'} and
            len(members)>=3 and all(all(v['bbox'][i]>=n['bbox'][i]-.75 and v['bbox'][i]+v['bbox'][i+2]<=n['bbox'][i]+n['bbox'][i+2]+.75 for i in (0,1)) for v in members if v is not n)]
        if representatives:
            representative=min(representatives,key=lambda n:(n['bbox'][2]*n['bbox'][3],n['node_id']))
            remaining=[n for n in tree['nodes'] if n['parent_id']==parent_id and n['node_id'] not in ids]
            return {'action':'promote_existing_lower_region','upper_id':parent_id,'representative_id':representative['node_id'],
                'upper_level':validate(tree)[parent_id]+1,'new_region_bbox':bbox,
                'remaining_upper_bbox':union([n['bbox'] for n in remaining]) if remaining else None,
                'remaining_members':[n['node_id'] for n in remaining],'new_region_parent_id':parent['parent_id'],'added_depth':0},None
    needed,reason=necessary_group(tree,members,bbox,policy)
    if not needed:return None,reason
    remaining=[n for n in tree['nodes'] if n['parent_id']==parent_id and n['node_id'] not in ids]
    if not remaining:return None,'reuse_existing_parent_instead'
    return {'action':'partition_existing_upper_region','upper_id':parent_id,
        'upper_level':validate(tree)[parent_id]+1,'new_region_bbox':bbox,
        'remaining_upper_bbox':union([n['bbox'] for n in remaining]),
        'remaining_members':[n['node_id'] for n in remaining],
        'new_region_parent_id':parent['parent_id'],'added_depth':0},None


def apply_replacement(tree, ids, group_id, kind, policy=None):
    """Replace part of an upper wrapper by a sibling region; preserve atomic geometry and provenance."""
    plan,reason=replacement_plan(tree,ids,policy)
    if plan is None:raise ValueError('Replacement not applicable: '+reason)
    if plan['action']=='promote_existing_lower_region':
        return _promote_existing(tree,ids,kind,plan)
    old_depths=validate(tree);parent=next(n for n in tree['nodes'] if n['node_id']==plan['upper_id'])
    old_box=list(parent['bbox'])
    # Reuse established group creation and parent-link validation; then promote the group
    # to the old wrapper's own level. The old wrapper becomes the residual visual region.
    operations=[{'op':'group','nodes':ids,'id':group_id,'reason':'vlm_candidate_'+kind},
        {'op':'reparent','node':group_id,'parent':parent['parent_id'],'reason':'rule_partition_upper_without_new_layer'}]
    result,trail=apply_operations(tree,operations)
    index={n['node_id']:n for n in result['nodes']};upper=index[parent['node_id']]
    result.setdefault('postprocess_original_geometry',{}).setdefault(upper['node_id'],{
        'bbox':old_box,'layout_bbox':deepcopy(parent.get('layout_bbox'))})
    upper['bbox']=list(plan['remaining_upper_bbox']);upper['layout_bbox']=list(upper['bbox'])
    upper['bbox_basis']='remaining_children_union';upper['postprocess_region_partitioned']=True
    upper['source_node_ids']=list(dict.fromkeys(s for k in plan['remaining_members'] for s in index[k].get('source_node_ids',[k])))
    index[group_id]['region_type']=kind
    index[group_id]['upper_region_replaced']=upper['node_id']
    trail.append({'op':'resize_upper_region','node':upper['node_id'],'before_bbox':old_box,
        'after_bbox':list(upper['bbox']),'remaining_members':plan['remaining_members'],
        'reason':'rule_retain_unselected_upper_contents'})
    result.setdefault('region_replacements',[]).append({**plan,'new_region_id':group_id,'members':list(ids),'upper_before_bbox':old_box})
    new_depths=validate(result)
    if any(new_depths[k]>d for k,d in old_depths.items() if k in new_depths):
        raise ValueError('Replacement would deepen an existing node')
    return result,trail


def _promote_existing(tree,ids,kind,plan):
    """Reuse a lower enclosing frame as the upper region instead of creating another wrapper."""
    index={n['node_id']:n for n in tree['nodes']};parent=index[plan['upper_id']];key=plan['representative_id'];old_depths=validate(tree)
    ops=[{'op':'reparent','node':member,'parent':key,'reason':'vlm_approved_reuse_existing_lower_region'} for member in ids if member!=key]
    if plan['remaining_members']:
        ops.append({'op':'reparent','node':key,'parent':parent['parent_id'],'reason':'rule_promote_existing_region_without_extra_layer'})
    else:
        ops.append({'op':'contract','node':parent['node_id'],'reason':'rule_replace_whole_upper_by_existing_lower'})
    result,trail=apply_operations(tree,ops);active={n['node_id']:n for n in result['nodes']}
    geometry=result.setdefault('postprocess_original_geometry',{})
    geometry.setdefault(key,{'bbox':list(index[key]['bbox']),'layout_bbox':deepcopy(index[key].get('layout_bbox'))})
    active[key].update(bbox=list(plan['new_region_bbox']),layout_bbox=list(plan['new_region_bbox']),region_type=kind,upper_region_replaced=parent['node_id'])
    if plan['remaining_members']:
        upper=active[parent['node_id']];geometry.setdefault(upper['node_id'],{'bbox':list(parent['bbox']),'layout_bbox':deepcopy(parent.get('layout_bbox'))})
        upper.update(bbox=list(plan['remaining_upper_bbox']),layout_bbox=list(plan['remaining_upper_bbox']),bbox_basis='remaining_children_union',postprocess_region_partitioned=True)
        trail.append({'op':'resize_upper_region','node':upper['node_id'],'before_bbox':parent['bbox'],'after_bbox':upper['bbox'],'reason':'rule_retain_unselected_upper_contents'})
    result.setdefault('region_replacements',[]).append({**plan,'new_region_id':key,'members':list(ids),'upper_before_bbox':list(parent['bbox'])})
    after=validate(result)
    if any(after[k]>d for k,d in old_depths.items() if k in after):raise ValueError('Existing-region promotion deepened a source node')
    return result,trail
