"""Rule-owned sibling candidates and restricted VLM grouping decisions in CSS pixels."""
from copy import deepcopy
from layout import partition
from rules import apply_operations, union, validate


def candidates(tree, policy=None):
    """Enumerate spatial/heading groups at every retained depth, reserving four-level budget."""
    policy = policy or {}
    depths = validate(tree, 4)
    nodes = {n['node_id']: n for n in tree['nodes']}
    families = {}
    for n in nodes.values():
        families.setdefault(n['parent_id'], []).append(n)
    bottom = dict(depths)
    for n in sorted(nodes.values(), key=lambda n: depths[n['node_id']], reverse=True):
        if n['parent_id'] is not None:
            bottom[n['parent_id']] = max(bottom[n['parent_id']], bottom[n['node_id']])
    result, seen = [], set()
    limit = policy.get('max_candidates', 64)
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError('grouping.max_candidates must be an integer in 1..256')
    def add(group, reason):
        ids = tuple(sorted(n['node_id'] for n in group))
        if len(ids) < 2 or ids in seen or len(result) >= limit:
            return
        if any(bottom[k] >= 3 for k in ids):
            return  # A new parent must not push any descendant beyond L4.
        parent = group[0]['parent_id']
        bbox = union([n['bbox'] for n in group])
        if parent is not None:
            p = nodes[parent]['bbox']
            if bbox[0] < p[0]-.5 or bbox[1] < p[1]-.5 or bbox[0]+bbox[2] > p[0]+p[2]+.5 or bbox[1]+bbox[3] > p[1]+p[3]+.5:
                return
        else:
            p = [0, 0, tree['capture']['width'], tree['capture']['height']]
            if bbox[2]*bbox[3]/(p[2]*p[3]) > policy.get('layer1_max_area_ratio', .6):
                return
        if all(abs(bbox[i]/p[i]-1) <= .02 for i in (2, 3)):
            return  # Avoid introducing another redundant wrapper.
        seen.add(ids)
        result.append({'candidate_id': f'g{len(result)+1:03d}', 'parent_id': parent,
            'members': list(ids), 'bbox': bbox, 'basis': reason})
    # Interleave families rather than exhausting the candidate cap on one large list.
    batches = []
    for parent, siblings in families.items():
        ordered = sorted(siblings, key=lambda n: (n['bbox'][1], n['bbox'][0], n['node_id']))
        proposed = [(g, 'spatial_gap_cluster') for g in partition(ordered, policy) if len(g) >= 2]
        for pos, n in enumerate(ordered):
            if n.get('text_role') == 'heading' or n.get('subtype') in ('heading', 'h1', 'h2', 'h3', 'h4'):
                end = pos+1
                while end < len(ordered) and end-pos < 8:
                    other = ordered[end]
                    if other.get('text_role') == 'heading' or other.get('subtype') in ('heading', 'h1', 'h2', 'h3', 'h4'):
                        break
                    end += 1
                proposed.insert(0, (ordered[pos:end], 'heading_and_following_content'))
        for pos in range(len(ordered)-1):
            a, b = ordered[pos:pos+2]
            gap = max(0, b['bbox'][1]-a['bbox'][1]-a['bbox'][3])
            x_overlap = min(a['bbox'][0]+a['bbox'][2], b['bbox'][0]+b['bbox'][2])-max(a['bbox'][0], b['bbox'][0])
            if gap <= policy.get('group_max_gap_px', 48) and x_overlap > 0:
                proposed.append(([a, b], 'adjacent_vertical_pair'))
        batches.append(proposed)
    for i in range(max((len(b) for b in batches), default=0)):
        for batch in batches:
            if i < len(batch):
                add(*batch[i])
    return result


def apply_decisions(tree, proposed, response, overflow_policy="reject"):
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
    for item in response['groups']:
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
            updated, trail = apply_operations(result, [op])
            validate(updated, 4 if overflow_policy == "reject" else None)
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
