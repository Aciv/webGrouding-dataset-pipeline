"""Rule-owned sibling candidates and restricted VLM grouping decisions in CSS pixels."""
from copy import deepcopy
from layout import partition
from rules import apply_operations, union, validate
from semantic_cleanup import necessary_group, top_down_cleanup
from replacement_groups import replacement_plan, apply_replacement
from layer_accumulation import accumulation_metrics


def candidates(tree, policy=None):
    """Enumerate spatial/heading groups at every retained depth, reserving four-level budget."""
    policy = policy or {}
    depths = validate(tree)
    nodes = {n['node_id']: n for n in tree['nodes']}
    families = {}
    for n in nodes.values():
        families.setdefault(n['parent_id'], []).append(n)
    bottom = dict(depths)
    for n in sorted(nodes.values(), key=lambda n: depths[n['node_id']], reverse=True):
        if n['parent_id'] is not None:
            bottom[n['parent_id']] = max(bottom[n['parent_id']], bottom[n['node_id']])
    density = accumulation_metrics(tree, policy) if policy.get('density_triggered_only') else {}
    result, seen = [], set()
    limit = policy.get('max_candidates', 64)
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError('grouping.max_candidates must be an integer in 1..256')
    def add(group, reason):
        ids = tuple(sorted(n['node_id'] for n in group))
        if len(ids) < 2 or ids in seen or len(result) >= limit:
            return
        if policy.get('group_action') != 'replace_upper' and any(bottom[k] >= 3 for k in ids):
            return  # A new parent must not push any descendant beyond L4.
        parent = group[0]['parent_id']
        bbox = union([n['bbox'] for n in group])
        necessity = 'legacy_spatial_candidate'
        if policy.get('density_triggered_only') and not density.get(parent, {}).get('triggers'):
            return
        early_plan = None
        if policy.get('prefer_existing_region_replacement'):
            early_plan, _ = replacement_plan(tree, list(ids), policy)
            if early_plan is None:
                return
            necessity = 'density_triggered_upper_replacement'
        if policy.get('conservative_grouping', False) and early_plan is None:
            needed, necessity = necessary_group(tree, group, bbox, policy)
            if not needed:
                return
        if parent is not None:
            p = nodes[parent]['bbox']
            if bbox[0] < p[0]-.5 or bbox[1] < p[1]-.5 or bbox[0]+bbox[2] > p[0]+p[2]+.5 or bbox[1]+bbox[3] > p[1]+p[3]+.5:
                return
        else:
            p = [0, 0, tree['capture']['width'], tree['capture']['height']]
            if bbox[2]*bbox[3]/(p[2]*p[3]) > policy.get('layer1_max_area_ratio', .6):
                return
        if early_plan is None and all(abs(bbox[i]/p[i]-1) <= .02 for i in (2, 3)):
            return  # Avoid introducing another redundant wrapper.
        plan = early_plan
        if policy.get('group_action') == 'replace_upper':
            plan, _ = replacement_plan(tree, list(ids), policy)
            if plan is None:
                return
        seen.add(ids)
        result.append({'candidate_id': f'g{len(result)+1:03d}', 'parent_id': parent,
            'members': list(ids), 'bbox': bbox, 'basis': reason,
            'necessity': necessity, 'existing_parent_member_count': sum(n['parent_id'] == parent for n in nodes.values()),
            'parent_subtype': nodes[parent].get('subtype') if parent else 'virtual_page',
            'parent_bbox': nodes[parent]['bbox'] if parent else [0,0,tree['capture']['width'],tree['capture']['height']],
            'replacement_plan': plan, 'aggregation_trigger': density.get(parent)})
    # Interleave families rather than exhausting the candidate cap on one large list.
    batches = []
    for parent, siblings in sorted(families.items(), key=lambda item: (-depths.get(item[0], -1), str(item[0]))):
        ordered = sorted(siblings, key=lambda n: (n['bbox'][1], n['bbox'][0], n['node_id']))
        proposed = [(g, 'spatial_gap_cluster') for g in partition(ordered, policy) if len(g) >= 2]
        if policy.get('prefer_existing_region_replacement'):
            for region in ordered:
                if region.get('struct') not in {'block','region','list','table'}:
                    continue
                b=region['bbox']
                contained=[v for v in ordered if v['node_id']==region['node_id'] or all(v['bbox'][i]>=b[i]-.75 and v['bbox'][i]+v['bbox'][i+2]<=b[i]+b[i+2]+.75 for i in (0,1))]
                if len(contained)>=3:
                    proposed.insert(0,(contained,'reuse_existing_enclosing_lower_region'))
        for pos, n in enumerate(ordered):
            if n.get('text_role') == 'heading' or n.get('subtype') in ('heading', 'h1', 'h2', 'h3', 'h4'):
                end = pos+1
                while end < len(ordered) and end-pos < 8:
                    other = ordered[end]
                    if other.get('text_role') == 'heading' or other.get('subtype') in ('heading', 'h1', 'h2', 'h3', 'h4'):
                        break
                    end += 1
                following = ordered[pos:end]
                if policy.get('conservative_grouping', False):
                    b = n['bbox']
                    following = [v for v in following if min(v['bbox'][0]+v['bbox'][2],b[0]+b[2]) > max(v['bbox'][0],b[0])]
                proposed.insert(0, (following, 'heading_and_following_content'))
        for pos in range(len(ordered)-1):
            a, b = ordered[pos:pos+2]
            gap = max(0, b['bbox'][1]-a['bbox'][1]-a['bbox'][3])
            x_overlap = min(a['bbox'][0]+a['bbox'][2], b['bbox'][0]+b['bbox'][2])-max(a['bbox'][0], b['bbox'][0])
            complementary = (a.get('text_role') == 'heading' or a.get('subtype') == 'heading' or
                ({a['struct'], b['struct']} >= {'image', 'text'}))
            if gap <= policy.get('group_max_gap_px', 48) and x_overlap > 0 and (not policy.get('conservative_grouping', False) or complementary):
                proposed.append(([a, b], 'adjacent_vertical_pair'))
        batches.append(proposed)
    for i in range(max((len(b) for b in batches), default=0)):
        for batch in batches:
            if i < len(batch):
                add(*batch[i])
    return result


def apply_decisions(tree, proposed, response, overflow_policy="reject", semantic_policy=None):
    """Apply approved groups; omit >L4 detail explicitly, preserving provenance and ignore masks."""
    if not isinstance(response, dict) or set(response)-{'groups'} or not isinstance(response.get('groups'), list):
        raise ValueError('Grouping response must contain only groups; arbitrary operations are forbidden')
    if overflow_policy not in ('reject', 'drop'):
        raise ValueError('depth_overflow_policy must be reject or drop')
    choices = {g['candidate_id']: g for g in proposed}
    if len(response['groups']) > len(proposed):
        raise ValueError('Too many grouping decisions')
    result, applied, rejected, decisions = deepcopy(tree), [], [], []
    seen, used = set(), set()
    decisions_in_order = response['groups']
    if semantic_policy is not None and semantic_policy.get('group_action') == 'replace_upper':
        # Provider array order must not determine whether a deep repair can execute.
        # Reuse the fixed plans; each plan is checked again against the active tree.
        def bottom_up_key(item):
            key = item.get('candidate_id') if isinstance(item, dict) else None
            return -choices.get(key, {}).get('replacement_plan', {}).get('upper_level', 0) if isinstance(key, str) else 0
        decisions_in_order = sorted(decisions_in_order, key=bottom_up_key)
    for item in decisions_in_order:
        try:
            if not isinstance(item, dict) or set(item)-{'candidate_id', 'decision', 'members', 'region_type'}:
                raise ValueError('Unexpected grouping fields')
            key = item.get('candidate_id')
            if key not in choices or key in seen:
                raise ValueError('Unknown or duplicate candidate')
            seen.add(key)
            decision = item.get('decision')
            if decision not in ('accept', 'reject', 'uncertain'):
                raise ValueError('Invalid decision')
            if decision != 'accept':
                decisions.append(item)
                continue
            ids = item.get('members', choices[key]['members'])
            if not isinstance(ids, list) or any(not isinstance(k, str) for k in ids) or len(ids) < 2 or len(set(ids)) != len(ids) or not set(ids) <= set(choices[key]['members']):
                raise ValueError('Members must be a distinct subset of the rule candidate')
            if semantic_policy is not None:
                active = {n['node_id']: n for n in result['nodes']}
                members = [active[k] for k in ids]
                if semantic_policy.get('prefer_existing_region_replacement'):
                    plan, _ = replacement_plan(result, ids, semantic_policy)
                    needed = plan is not None
                else:
                    needed, _ = necessary_group(result, members, union([n['bbox'] for n in members]), semantic_policy)
                if not needed:
                    decisions.append(item)
                    continue
            if used.intersection(ids):
                raise ValueError('Overlapping approved groups; member already grouped')
            requested_kind = item.get('region_type', 'unknown')
            kind = requested_kind
            if kind not in ('section', 'navigation', 'card', 'image_with_caption', 'heading_content', 'table_group', 'form_group', 'footer_group', 'visual_group'):
                kind = 'unknown'
            generated_id = 'vlm_group_'+key
            occupied = {n['node_id'] for n in result['nodes']} | set(result.get('postprocess_sources', {}))
            while generated_id in occupied:
                generated_id += '_new'
            op = {'op': 'group', 'nodes': ids, 'id': generated_id, 'reason': 'vlm_candidate_'+kind, 'region_type': kind, 'requested_region_type': requested_kind}
            if semantic_policy is not None and semantic_policy.get('group_action') == 'replace_upper':
                plan, reason = replacement_plan(result, ids, semantic_policy)
                if plan is None:
                    decisions.append(item)
                    continue
                updated, trail = apply_replacement(result, ids, generated_id, kind, semantic_policy)
                generated_id = plan.get('representative_id', generated_id)
                trail[0].update(region_type=kind, requested_region_type=requested_kind)
            else:
                updated, trail = apply_operations(result, [op])
            if semantic_policy is not None:
                updated, compressed, _ = top_down_cleanup(updated, semantic_policy, compress_only=True)
                trail.extend(compressed)
            limit = 4 if overflow_policy == 'reject' else None
            # A preserved deep baseline is a review candidate; a new group may not make it deeper.
            if semantic_policy is not None:
                limit = max(3, max(validate(result).values(), default=0))+1 if limit else None
            validate(updated, limit)
            next(n for n in updated["nodes"] if n["node_id"] == generated_id)["region_type"] = kind
            result = updated
            used.update(ids)
            applied.extend(trail)
            decisions.append(item)
        except (ValueError, TypeError, KeyError) as error:
            rejected.append({'decision': item, 'error': str(error)})
    answered = {d['candidate_id'] for d in decisions}
    unresolved = [g['candidate_id'] for g in proposed if g['candidate_id'] not in answered]
    unresolved += [d['candidate_id'] for d in decisions if d['decision'] == 'uncertain']
    if overflow_policy == "drop":
        result, omitted = prune_depth(result)
        applied.extend(omitted)
    return result, applied, rejected, sorted(set(unresolved))


def prune_depth(tree, max_levels=4):
    """Omit deepest leaves beyond the training depth; keep source facts and ignore-region bbox."""
    result, trail = deepcopy(tree), []
    depths = validate(result)
    overflow = sorted((n for n in result['nodes'] if depths[n['node_id']] >= max_levels),
        key=lambda n: (-depths[n['node_id']], n['node_id']))
    for node in overflow:
        op = {'op': 'drop', 'node': node['node_id'],
            'reason': 'rule_detail_beyond_four_levels_after_grouping',
            'omitted_level': depths[node['node_id']]+1,
            'omitted_bbox': list(node['bbox']), 'omitted_struct': node.get('struct'),
            'omitted_generated': bool(node.get('postprocess_generated'))}
        result, applied = apply_operations(result, [op])
        trail.extend(applied)
    validate(result, max_levels)
    return result, trail
