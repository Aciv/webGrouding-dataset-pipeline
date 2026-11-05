"""Top-down saved-tree cleanup and conservative semantic grouping gates; CSS xywh only."""
from collections import Counter
from rules import apply_operations, validate, overlap
from deep_noise import classify_deep_noise

GENERIC = {'', 'content_group', 'layout_group', 'visual_group', 'visual_block', 'mixed_text', 'heading_group'}


def generic_wrapper(node):
    """Identify recorded generic block wrappers; named components/regions are not generic."""
    return node.get('struct') == 'block' and node.get('subtype', '') in GENERIC


def low_value_small(node, policy):
    """Flag tiny empty/decorative candidates only; text, media and actionable controls survive."""
    w, h = node['bbox'][2:]
    if w*h > policy.get('deep_small_area_px2', 256) or max(w, h) > policy.get('deep_small_max_side_px', 32):
        return False
    if node.get('disposition') == 'review' or node.get('interaction_role') or node.get('text_role'):
        return False
    if node.get('struct') in {'text', 'image', 'media', 'control', 'table', 'list', 'region'}:
        return False
    return generic_wrapper(node) or node.get('struct') == 'unknown' or (
        node.get('struct') == 'graphic' and node.get('subtype') in {'icon', 'decoration'})


def top_down_cleanup(tree, policy=None, *, compress_only=False):
    """Inspect shallow-to-deep, recomputing levels after every contraction; never drop a subtree.

    Sparse L5+ generic wrappers are contracted, not deleted together with their children.
    Tiny decorative leaves are omitted only in deep families with a meaningful representative.
    A sparse layer alone never justifies deleting text, images or controls.
    """
    policy = policy or {}
    result, _ = apply_operations(tree, [])
    trail, scans = [], []
    strict_summary = None
    if not compress_only and policy.get('deep_noise_mode') == 'strict':
        marked, evidence, strict_summary = classify_deep_noise(result, policy, generic_wrapper)
        # Classification uses original depths, then execution removes only current leaves,
        # deepest first. No descendant disappears implicitly with a container.
        pending=set(marked)
        while pending:
            index={n['node_id']:n for n in result['nodes']}
            parents={n['parent_id'] for n in result['nodes']}
            available=[k for k in pending if k in index and k not in parents]
            if not available:
                raise ValueError('Strict noise selection is not a removable leaf closure')
            depths=validate(result)
            for key in sorted(available,key=lambda k:(-depths[k],k)):
                op={'op':'drop','node':key,'reason':'rule_strict_'+marked[key], **evidence[key]}
                # reason must retain the stable rule prefix rather than the evidence tag.
                op['reason']='rule_strict_'+marked[key]
                result,applied=apply_operations(result,[op]);trail.extend(applied);pending.remove(key)
    while True:
        index = {n['node_id']: n for n in result['nodes']}
        children = {}
        depths = validate(result)
        for n in index.values():
            children.setdefault(n['parent_id'], []).append(n)
        counts = Counter(depths.values())
        deep_ancestors = set()
        for n in index.values():
            if depths[n['node_id']] < 4:
                continue
            p = n['parent_id']
            while p is not None:
                deep_ancestors.add(p)
                p = index[p]['parent_id']
        operation = None
        for n in sorted(index.values(), key=lambda n: (depths[n['node_id']], n['node_id'])):
            key = n['node_id']; level = depths[key]+1; kids = children.get(key, [])
            if generic_wrapper(n) and kids:
                sparse_deep = level >= 5 and counts[depths[key]] <= policy.get('deep_sparse_count', 2)
                near_child = len(kids) == 1 and overlap(n['bbox'], kids[0]['bbox']) >= policy.get('wrapper_child_iou', .85)
                # After a model group, an old generic wrapper with exactly one group child
                # carries no independent parent assignment and may be replaced by that group.
                unary_deep = key in deep_ancestors and len(kids) == 1
                if sparse_deep or (level >= 5 and near_child) or unary_deep:
                    operation = {'op': 'contract', 'node': key, 'reason': 'rule_top_down_sparse_wrapper' if sparse_deep else 'rule_top_down_redundant_depth_wrapper'}
                    break
            if not compress_only and level >= 5 and not kids and low_value_small(n, policy):
                parent = index.get(n['parent_id'])
                peers = children.get(n['parent_id'], [])
                protected_parent = parent and parent.get('struct') in {'control', 'image', 'media'}
                # Do not erase a branch consisting solely of unclassified tiny symbols.
                represented = parent and (not low_value_small(parent, policy) or any(not low_value_small(v, policy) for v in peers))
                if represented and not protected_parent:
                    operation = {'op': 'drop', 'node': key, 'reason': 'rule_top_down_deep_tiny_low_value', 'source_level': level}
                    break
        if operation is None:
            break
        result, applied = apply_operations(result, [operation]); trail.extend(applied)
    depths = validate(result)
    for level in sorted(set(depths.values())):
        members = [n for n in result['nodes'] if depths[n['node_id']] == level]
        scans.append({'level': level+1, 'count': len(members), 'all_small_low_value': all(low_value_small(n, policy) for n in members)})
    summary = {'direction': 'top_down', 'strict_original_depth_noise': strict_summary, 'layer_scan': scans,
        'operations': dict(Counter(op['reason'] for op in trail)),
        'meaningful_deep_retained': [n['node_id'] for n in result['nodes'] if depths[n['node_id']] >= 4 and not low_value_small(n, policy)]}
    return result, trail, summary


def necessary_group(tree, members, bbox, policy=None):
    """Require a missing subgroup, not merely coherent siblings already represented by a parent."""
    policy = policy or {}
    index = {n['node_id']: n for n in tree['nodes']}
    parent = members[0]['parent_id']
    siblings = [n for n in tree['nodes'] if n['parent_id'] == parent]
    if len(members) == len(siblings):
        return False, 'existing_parent_already_represents_all_members'
    if parent is not None:
        p = index[parent]['bbox']
        if bbox[2]*bbox[3]/(p[2]*p[3]) >= policy.get('group_parent_coverage_limit', .85):
            return False, 'existing_parent_already_covers_region'
    member_ids = {n['node_id'] for n in members}
    intrusion = 0
    for n in siblings:
        if n['node_id'] in member_ids:
            continue
        b = n['bbox']
        intrusion += max(0, min(b[0]+b[2],bbox[0]+bbox[2])-max(b[0],bbox[0]))*max(0,min(b[1]+b[3],bbox[1]+bbox[3])-max(b[1],bbox[1]))
    if intrusion/(bbox[2]*bbox[3]) > policy.get('group_max_intrusion_ratio', .15):
        return False, 'unrelated_sibling_inside_group_boundary'
    return True, 'proper_subgroup_missing_from_existing_parent'
