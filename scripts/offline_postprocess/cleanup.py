"""Deterministic component-level supervision, independent of pixels and persistence."""
from collections import defaultdict
from copy import deepcopy
from .domain import area, contains, intersection, union, graph_depths

_COMPONENTS = {'article', 'card', 'nav', 'navigation', 'footer', 'header', 'form', 'list', 'table', 'section'}


def important(node):
    """Protect explicit headings, buttons, input controls and substantial foreground media."""
    subtype = node.get('subtype') or ''
    box = node['bbox']
    return (node.get('text_role') == 'heading' or subtype in {'heading', 'button', 'input_text', 'textarea', 'select', 'checkbox', 'radio'}
            or node.get('interaction_role') in {'button', 'textbox', 'combobox', 'checkbox', 'radio', 'slider'}
            or (node.get('struct') in {'image', 'media'} and subtype != 'background_image'
                and box[2]-box[0] >= 48 and box[3]-box[1] >= 48))


def critical_target(node):
    """Prioritize existing atomic content and evidenced paragraphs over dispensable container boundaries."""
    return (important(node) or node.get('struct') in {'text', 'control', 'graphic', 'media'}
            or node.get('struct') == 'image' and node.get('subtype') != 'background_image'
            or node.get('content_representative_basis') == 'source_paragraph_and_inline_content')


def fine_grained(node, policy):
    """Identify task-excluded small atoms using dimensions and roles, never size alone for key controls."""
    if important(node):
        return False
    box = node['bbox']
    w, h = box[2]-box[0], box[3]-box[1]
    if node.get('struct') in {'image', 'graphic'}:
        return w <= policy['tiny_side'] and h <= policy['tiny_side']
    if node.get('struct') == 'text':
        return h <= policy['fine_text_height'] and w <= policy['fine_width']
    return node.get('subtype') in {'link', 'menuitem'} and h <= policy['fine_link_height'] and w <= policy['fine_width']


def clean_nodes(source_nodes, width, height, policy, max_height, trigger_height=2700, region_proposals=()):
    """Return copied stable graph and auditable eligibility edits in original CSS xyxy.

    Source edges remain available. Generated groups only reparent siblings, with every
    edge edit recorded; slicing must not alter the returned graph.
    """
    nodes = deepcopy(source_nodes)
    index = {n['node_id']: n for n in nodes}
    operations = []
    initially_important = {n['node_id'] for n in nodes if n['detection_eligible'] and important(n)}

    def disable(node, reason, representative=None, stage='small_box_bottom_up'):
        if not node['detection_eligible']:
            return
        node.update(detection_eligible=False, supervision_reason=reason)
        if representative:
            node['represented_by'] = representative['global_node_id']
        operations.append({'stage': stage, 'op': 'exclude_supervision', 'global_node_id': node['global_node_id'],
                           'reason': reason, 'represented_by': node.get('represented_by'), 'original_bbox': list(node['bbox'])})

    # Merge only exact sibling duplicates with the same semantic signature and source identity.
    seen = {}
    for node in sorted(nodes, key=lambda n: n['node_id']):
        if not node['detection_eligible']:
            continue
        signature = (node.get('parent_id'), node.get('struct'), node.get('subtype'), node.get('semantic_role'),
                     node.get('interaction_role'), node.get('text_role'), tuple(node['bbox']),
                     tuple(sorted(node.get('source_node_ids') or [])))
        if signature in seen:
            disable(node, 'duplicate_same_source', seen[signature])
        else:
            seen[signature] = node

    # A paragraph represented in the source graph must not vanish when its only
    # independent child is a small inline link. This reuses the source bbox/role;
    # no text GT is inferred from pixels.
    for candidate in nodes:
        b = candidate['bbox']
        if (candidate['detection_eligible'] or candidate.get('tag') not in {'p', 'blockquote', 'figcaption'}
                or not 32 <= b[3]-b[1] <= policy['group_max_height']):
            continue
        inline = [n for n in nodes if n.get('parent_id') == candidate['node_id']
                  and n['detection_eligible'] and contains(b, n['bbox'])
                  and (n.get('struct') == 'text' or n.get('subtype') == 'link')]
        if inline:
            candidate.update(detection_eligible=True, supervision_reason='existing_text_representative',
                             content_representative_basis='source_paragraph_and_inline_content')
            operations.append({'stage': 'small_box_bottom_up', 'op': 'activate_representative',
                               'global_node_id': candidate['global_node_id'],
                               'reason': 'source_paragraph_and_inline_content',
                               'members': [n['global_node_id'] for n in inline]})

    fine = [n for n in nodes if n['detection_eligible'] and fine_grained(n, policy)]
    assigned = defaultdict(list)
    unassigned = defaultdict(list)
    for node in fine:
        parent = node.get('parent_id')
        found = None
        while parent is not None:
            candidate = index[parent]
            b = candidate['bbox']
            semantic = (candidate.get('subtype') in _COMPONENTS or candidate.get('struct') in {'list', 'table'}
                        or candidate.get('content_representative_basis') == 'source_paragraph_and_inline_content')
            compact = b[3]-b[1] <= policy['group_max_height'] and area(b) <= width*height*.65
            if semantic and compact and contains(b, node['bbox']):
                found = candidate
                break
            parent = candidate.get('parent_id')
        if found:
            assigned[found['node_id']].append(node)
        else:
            unassigned[node.get('parent_id')].append(node)

    def use_representative(representative, members):
        minimum = 1 if representative.get('content_representative_basis') == 'source_paragraph_and_inline_content' else policy['group_min_members']
        if len(members) < minimum:
            return False
        if not representative['detection_eligible']:
            representative.update(detection_eligible=True, supervision_reason='existing_content_representative')
            operations.append({'stage': 'small_box_bottom_up', 'op': 'activate_representative',
                               'global_node_id': representative['global_node_id'],
                               'reason': 'existing_content_representative',
                               'members': [n['global_node_id'] for n in members]})
        for member in members:
            disable(member, 'represented_by_content_group', representative)
        return True

    for key in sorted(assigned):
        members = assigned[key]
        if not use_representative(index[key], members):
            for member in members:
                unassigned[member.get('parent_id')].append(member)

    def create_group(members, parent, subtype='content_group', stage='small_box_bottom_up'):
        key = f'component_group_{len(nodes):06d}'
        while key in index:
            key += '_new'
        observation_id = members[0]['global_node_id'].rsplit('/', 1)[0]
        group = {'node_id': key, 'global_node_id': f'{observation_id}/{key}', 'parent_id': parent,
                 'source_parent_id': parent, 'source_layer': None,
                 'source_node_ids': sorted({s for n in members for s in n.get('source_node_ids', [n['node_id']])}),
                 'struct': 'block', 'subtype': subtype, 'semantic_role': None,
                 'bbox': union([n['bbox'] for n in members]), 'bbox_format': 'xyxy',
                 'detection_eligible': True, 'source_detection_eligible': False,
                 'supervision_reason': 'generated_content_representative', 'generated': True,
                 'pixel_visibility': 'unknown', 'occlusion': 'unknown'}
        nodes.append(group)
        index[key] = group
        changes = []
        for member in members:
            # Only sibling groups change edges; large mixed-region views keep global source edges.
            if member.get('parent_id') == parent:
                changes.append({'node_id': member['node_id'], 'before': parent, 'after': key})
                member['parent_id'] = key
        operations.append({'stage': stage, 'op': 'create_content_group', 'reason': subtype,
                           'global_node_id': group['global_node_id'], 'bbox': group['bbox'],
                           'members': [n['global_node_id'] for n in members], 'parent_changes': changes})
        return group

    # Local bounded runs: same source parent, aligned or tightly adjacent, no transitive full-page chains.
    for parent in sorted(unassigned, key=lambda x: '' if x is None else x):
        ordered = sorted(unassigned[parent], key=lambda n: (n['bbox'][1], n['bbox'][0], n['node_id']))
        clusters = []
        for node in ordered:
            box = node['bbox']
            selected = None
            for cluster in reversed(clusters[-12:]):
                bounds = union([n['bbox'] for n in cluster])
                combined = union([bounds, box])
                gap_y = max(0, box[1]-bounds[3], bounds[1]-box[3])
                gap_x = max(0, box[0]-bounds[2], bounds[0]-box[2])
                aligned = abs(box[0]-bounds[0]) <= 20 or min(box[2], bounds[2])-max(box[0], bounds[0]) > 0
                same_row = min(box[3], bounds[3])-max(box[1], bounds[1]) > 0
                if (combined[3]-combined[1] <= 240 and combined[2]-combined[0] <= min(width, 600)
                        and ((aligned and gap_y <= policy['group_gap']) or (same_row and gap_x <= policy['group_gap']))):
                    selected = cluster
                    break
            if selected is None:
                clusters.append([node])
            else:
                selected.append(node)
        for members in clusters:
            if len(members) >= policy['group_min_members']:
                # Reuse the actual parent only when it tightly expresses this group.
                existing = index.get(parent)
                tight = union([n['bbox'] for n in members])
                if (existing and contains(existing['bbox'], tight)
                        and area(existing['bbox']) <= area(tight)*1.6
                        and existing['bbox'][3]-existing['bbox'][1] <= policy['group_max_height']):
                    representative = existing
                else:
                    representative = create_group(members, parent)
                use_representative(representative, members)
            else:
                disable(members[0], 'drop_fine_grained_noise')

    # Local pixel partitions are applied before whole-page slicing. A region bbox
    # is always a union of existing targets, never the arbitrary pixel partition.
    for proposal in region_proposals:
        root = index[proposal['root_id']]
        if not root['detection_eligible']:
            continue
        protected = [n for n in nodes if n is not root and n['detection_eligible']
                     and contains(root['bbox'], n['bbox'])
                     and (critical_target(n) or n['bbox'][3]-n['bbox'][1] <= 700)]
        if any(not any(contains(zone, n['bbox']) for zone in proposal['zones']) for n in protected):
            operations.append({'stage': 'large_box_cleanup_split', 'op': 'reject_local_partition',
                               'global_node_id': root['global_node_id'], 'reason': 'crosses_cleaned_component'})
            continue
        parts = []
        for zone in proposal['zones']:
            members = [n for n in protected if contains(zone, n['bbox'])]
            if len(members) < 2:
                continue
            bounds = union([n['bbox'] for n in members])
            if bounds[3]-bounds[1] > max_height:
                continue
            parts.append((members, bounds))
        if len(parts) < 2:
            continue
        covered = {n['node_id'] for members, _ in parts for n in members}
        if covered != {n['node_id'] for n in protected}:
            continue
        representatives_local = []
        for members, bounds in parts:
            existing = next((n for n in members if n.get('struct') in {'block', 'region', 'list', 'table'}
                             and contains(n['bbox'], bounds) and area(n['bbox']) <= area(bounds)*1.1), None)
            representative = existing or create_group(members, root['node_id'], 'pixel_content_region', 'large_box_cleanup_split')
            representatives_local.append(representative['global_node_id'])
        disable(root, 'decomposed_local_region', stage='large_box_cleanup_split')
        operations.append({'stage': 'large_box_cleanup_split', 'op': 'apply_local_partition',
                           'global_node_id': root['global_node_id'], 'reason': proposal['basis'],
                           'pixel_cuts': proposal['cuts'], 'regions': representatives_local,
                           'partition_zones': proposal['zones']})

    # Remove meaningless large shells only with multiple independently labelled contents.
    # Spatial contents are checked even when the source DOM graph has no direct children.
    representatives = {n.get('represented_by') for n in nodes}
    for node in list(nodes):
        if not node['detection_eligible'] or node.get('generated') or node['global_node_id'] in representatives:
            continue
        box = node['bbox']
        h = box[3]-box[1]
        generic = node.get('subtype') in {'visual_block', 'background_image', 'section'}
        if not generic or h < height*policy['wrapper_height_ratio']:
            continue
        contents = [n for n in nodes if n is not node and n['detection_eligible']
                    and contains(box, n['bbox']) and area(n['bbox']) < area(box)*.8]
        if len(contents) >= 2:
            disable(node, 'drop_redundant_wrapper', stage='large_box_cleanup_split')

    # Large ordinary tables/lists: replace their broad target with row-gap based source groups.
    for node in list(nodes):
        if not node['detection_eligible'] or node.get('struct') not in {'table', 'list'}:
            continue
        box = node['bbox']
        if box[3]-box[1] <= max_height:
            continue
        contents = sorted([n for n in nodes if n is not node and n['detection_eligible']
                           and contains(box, n['bbox']) and area(n['bbox']) < area(box)*.7],
                          key=lambda n: (n['bbox'][1], n['node_id']))
        if len(contents) < 2:
            continue
        runs, run = [], []
        for child in contents:
            if run:
                bounds = union([n['bbox'] for n in run])
                # A source gap is required; do not turn arbitrary windows into object GT.
                if child['bbox'][1] > bounds[3]+1 and child['bbox'][3]-bounds[1] > max_height*.7:
                    runs.append(run)
                    run = []
            run.append(child)
        if run:
            runs.append(run)
        if len(runs) > 1 and all(union([n['bbox'] for n in group])[3]-union([n['bbox'] for n in group])[1] <= max_height for group in runs):
            for members in runs:
                if len(members) < 2:
                    continue
                group = create_group(members, node['node_id'], 'source_row_group', 'large_box_cleanup_split')
                for member in members:
                    if not important(member):
                        disable(member, 'represented_by_content_group', group, 'large_box_cleanup_split')
            disable(node, 'decomposed_large_region', stage='large_box_cleanup_split')

    if height > trigger_height:
        for node in nodes:
            if node['detection_eligible'] and node['bbox'][3]-node['bbox'][1] > max_height:
                disable(node, 'drop_oversized_target', stage='large_box_cleanup_split')
                node['incomplete_annotation_risk'] = node.get('subtype') == 'background_image'
    depths = graph_depths(nodes)
    for node in nodes:
        node['derived_depth'] = depths[node['node_id']]
        node['bbox_format'] = 'xyxy'
        if node.get('represented_by'):
            global_index = {n['global_node_id']: n for n in nodes}
            representative = global_index[node['represented_by']]
            visited = {node['global_node_id']}
            while not representative['detection_eligible'] and representative.get('represented_by'):
                if representative['global_node_id'] in visited:
                    raise ValueError('cyclic_content_representation')
                visited.add(representative['global_node_id'])
                representative = global_index[representative['represented_by']]
            if not representative['detection_eligible']:
                raise ValueError('inactive_content_representative')
            if node['represented_by'] != representative['global_node_id']:
                previous = node['represented_by']
                node['represented_by'] = representative['global_node_id']
                operations.append({'stage': 'large_box_cleanup_split', 'op': 'resolve_content_representative',
                                   'global_node_id': node['global_node_id'], 'before': previous,
                                   'after': node['represented_by']})
    lost_important = sorted(initially_important - {n['node_id'] for n in nodes if n['detection_eligible']})
    return nodes, operations, {'important_source_targets': len(initially_important),
                               'excluded_important_ids': lost_important,
                               'important_exclusion_is_not_verified_error': True}
