"""Publish final L1-L5 graph/crop labels after fusion, preserving L6+ details by adsorption.

L6+ nodes are not promoted wholesale. Large L6+ boxes absorb nearby small L6+ boxes,
then only owners not already represented by an L5 target are projected into L5.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from scripts.offline_postprocess.domain import VERSION, area, contains, graph_depths, intersection, union
from scripts.offline_postprocess.infrastructure import read_jsonl, write_json, write_jsonl, sha256_file
from scripts.offline_postprocess.labels import correct_pseudo_icon_label

RULE = 'final_component_layers_l6_backfill_v1'
GRAPH_RULE = f'{VERSION}_final_l5_l6_backfill'

PROMOTION_POLICY = {
    'rule_version': 'l6_large_box_absorption_v1',
    'scope': 'L6+ detail owners after full fusion',
    'large_min_area_px2': 2500,
    'large_min_side_px': 48,
    'small_max_area_ratio': 0.25,
    'small_max_area_px2': 25000,
    'neighbor_gap_px': 24,
    'l5_coverage': 0.95,
    'same_l4_ancestor': True,
    'promote_only_adsorbing_owners': True,
}


def box_gap(a, b):
    """Return the largest per-axis gap between two xyxy boxes; overlap or containment is zero."""
    return max(0.0, a[0]-b[2], b[0]-a[2], a[1]-b[3], b[1]-a[3])


def covered_fraction(inner, outer):
    """Return the fraction of inner xyxy area covered by outer; pure geometry, not semantics."""
    return area(intersection(inner, outer))/max(1.0, area(inner))


def nearest_depth_ancestor(node_id, depth, index, depths):
    """Walk the actual fused parent chain to the first ancestor at the requested depth."""
    parent = index[node_id].get('parent_id')
    while parent is not None:
        if depths[parent] == depth:
            return index[parent]
        parent = index[parent].get('parent_id')
    return None


def is_large_box(node, policy):
    """A box is a possible owner only when its area and both sides clear the absolute floor."""
    box = node['bbox']
    return area(box) >= policy['large_min_area_px2'] and min(box[2]-box[0], box[3]-box[1]) >= policy['large_min_side_px']


def promote_l6_details(graph, depths, policy):
    """Adsorb nearby small L6+ boxes into large owners, then keep only owners missing from L5.

    This is a label projection, not a new hierarchy: promoted owners are reattached to the
    nearest L4 ancestor and published at L5. Absorbed boxes stay audit metadata only.
    """
    index = {n['node_id']: n for n in graph['nodes']}
    global_index = {n['global_node_id']: n for n in graph['nodes']}
    deep = [n for n in graph['nodes'] if n.get('detection_eligible') and depths[n['node_id']] >= 5]
    existing_l5 = [n for n in graph['nodes'] if n.get('detection_eligible') and depths[n['node_id']] == 4]
    counts = Counter({'deep_candidates': len(deep)})
    if not deep:
        return [], counts

    families = defaultdict(list)
    for node in deep:
        ancestor = nearest_depth_ancestor(node['node_id'], 3, index, depths)
        if ancestor is not None:
            families[ancestor['global_node_id']].append(node)
        else:
            counts['skipped_no_l4_ancestor'] += 1

    promoted, promoted_boxes, absorbed = [], [], set()
    for ancestor_id in sorted(families):
        ancestor = global_index[ancestor_id]
        members = sorted(families[ancestor_id], key=lambda n: (-area(n['bbox']), depths[n['node_id']], n['node_id']))
        large_ids = {n['node_id'] for n in members if is_large_box(n, policy)}
        counts['large_owners'] += len(large_ids)
        for owner in members:
            owner_id = owner['node_id']
            if owner_id not in large_ids or owner_id in absorbed:
                continue
            owner_area = area(owner['bbox'])
            small = [n for n in members if n['node_id'] != owner_id and n['node_id'] not in large_ids
                     and n['node_id'] not in absorbed
                     and area(n['bbox']) <= owner_area*policy['small_max_area_ratio']
                     and area(n['bbox']) <= policy['small_max_area_px2']
                     and box_gap(owner['bbox'], n['bbox']) <= policy['neighbor_gap_px']]
            if not small:
                continue
            counts['adsorbing_groups'] += 1
            group_bbox = union([owner['bbox'], *[n['bbox'] for n in small]])
            if not contains(ancestor['bbox'], group_bbox):
                counts['skipped_outside_l4_ancestor'] += 1
                continue
            if max([covered_fraction(group_bbox, n['bbox']) for n in existing_l5] or [0]) >= policy['l5_coverage']:
                counts['skipped_covered_by_l5'] += 1
                absorbed.update(n['node_id'] for n in small)
                counts['absorbed_nodes'] += len(small)
                continue
            if max([covered_fraction(group_bbox, box) for box in promoted_boxes] or [0]) >= policy['l5_coverage']:
                counts['skipped_duplicate_promoted'] += 1
                absorbed.update(n['node_id'] for n in small)
                counts['absorbed_nodes'] += len(small)
                continue
            value = deepcopy(owner)
            value.update(parent_id=ancestor['node_id'], bbox=group_bbox, bbox_format='xyxy',
                         layer=4, derived_depth=4, promoted=True,
                         promotion_rule=policy['rule_version'],
                         promoted_from_depth=depths[owner_id], original_parent_id=owner.get('parent_id'),
                         owner_bbox=list(owner['bbox']),
                         absorbed_node_ids=sorted(n['global_node_id'] for n in small),
                         absorbed_bboxes=[list(n['bbox']) for n in small])
            value.pop('represented_by', None)
            promoted.append(value)
            promoted_boxes.append(group_bbox)
            absorbed.update(n['node_id'] for n in small)
            counts['absorbed_nodes'] += len(small)
            counts['promoted_owners'] += 1
    counts['unassigned_deep'] = sum(1 for n in deep if n['node_id'] not in absorbed) - counts['promoted_owners']
    return promoted, counts


def truncate_graph(graph, max_layers=5, promotion=None):
    """Keep true parent depths 0..max_layers-1, optionally replacing L6+ deletion with L5 adsorption.

    Without a promotion policy this remains the strict truncation used by historical
    callers. The current pipeline passes PROMOTION_POLICY to publish selected L6+ owners.
    """
    if type(max_layers) is not int or not 1 <= max_layers <= 5:
        raise ValueError('Final graph must retain between one and five layers')
    depths = graph_depths(graph['nodes'])
    kept = [deepcopy(n) for n in graph['nodes'] if depths[n['node_id']] < max_layers]
    promoted, promotion_counts = ([], Counter()) if promotion is None else promote_l6_details(graph, depths, promotion)
    identifiers = {n['global_node_id'] for n in kept+promoted}
    for node in kept:
        node['layer'] = node['derived_depth'] = depths[node['node_id']]
        if node.get('represented_by') and node['represented_by'] not in identifiers:
            node.pop('represented_by')
    nodes = kept+promoted
    result = {**graph, 'nodes':nodes, 'detection_target_ids':[n['global_node_id'] for n in nodes if n.get('detection_eligible')]}
    result['rule_version'] = GRAPH_RULE
    if promotion is not None:
        result['l6_detail_promotion'] = {'policy':dict(promotion), 'counts':dict(promotion_counts)}
    counts = {'nodes_before':len(graph['nodes']), 'nodes_after':len(nodes),
              'detection_targets_before':sum(bool(n.get('detection_eligible')) for n in graph['nodes']),
              'detection_targets_after':len(result['detection_target_ids']),
              **{f'l6_{key}':value for key, value in promotion_counts.items()}}
    return result, counts


def truncate_crop(crop, graph):
    """Project crop-local positives onto published nodes and promoted L5 owners.

    Absorbed L6+ targets leave the crop; their owner appears once with the union bbox
    clipped to the crop. Crop/core coordinates and ignore geometry are never changed.
    """
    nodes = {n['global_node_id']:n for n in graph['nodes']}
    promoted = {n['global_node_id']:n for n in graph['nodes'] if n.get('promoted')}
    absorbed = {identifier for n in promoted.values() for identifier in n.get('absorbed_node_ids', [])}
    owner_duplicates = {t['global_node_id']:t.get('duplicate_context_occurrence', False)
                        for t in crop['targets'] if t['global_node_id'] in promoted}
    value = deepcopy(crop)
    targets = []
    for target in crop['targets']:
        identifier = target['global_node_id']
        if identifier in absorbed or identifier in promoted:
            continue
        node = nodes.get(identifier)
        if node and node.get('detection_eligible'):
            targets.append({**target, 'layer':node['layer'], 'derived_depth':node['derived_depth']})
    box = crop['crop_bbox']
    for identifier, node in sorted(promoted.items()):
        clipped = intersection(box, node['bbox'])
        if area(clipped) <= 0:
            continue
        targets.append({
            'global_node_id':identifier,
            'global_parent_id':f"{identifier.rsplit('/', 1)[0]}/{node['parent_id']}",
            'bbox':[clipped[0]-box[0], clipped[1]-box[1], clipped[2]-box[0], clipped[3]-box[1]],
            'struct':node.get('struct'), 'subtype':node.get('subtype'), 'kind':node.get('kind'),
            'semantic_role':node.get('semantic_role'), 'text_role':node.get('text_role'),
            'source_node_ids':node.get('source_node_ids', []),
            'structure_reasons':node.get('structure_reasons', []),
            'layer':4, 'derived_depth':4,
            'promotion_rule':node.get('promotion_rule'),
            'promoted_from_depth':node.get('promoted_from_depth'),
            'owner_bbox':node.get('owner_bbox'),
            'absorbed_node_ids':node.get('absorbed_node_ids', []),
            'geometric_visibility':'complete' if contains(box, node['bbox']) else 'partial',
            'duplicate_context_occurrence':bool(owner_duplicates.get(identifier, False)),
        })
    value['targets'] = targets
    value['ignore_regions'] = [i for i in value['ignore_regions']
                              if (not i.get('global_node_id') or i['global_node_id'] in nodes)
                              and (not i.get('global_parent_id') or i['global_parent_id'] in nodes)]
    for key in ('target_union_coverage','detail_training_eligible','detail_training_discarded','detail_training_reason_codes'):
        value.pop(key,None)
    value['final_layer_projection'] = {'max_layers':5,
                                       'discarded_target_occurrences':len(crop['targets'])-len([t for t in crop['targets'] if t['global_node_id'] in nodes and nodes[t['global_node_id']].get('detection_eligible') and not nodes[t['global_node_id']].get('promoted') and t['global_node_id'] not in absorbed]),
                                       'promoted_target_occurrences':sum(1 for t in targets if t['global_node_id'] in promoted)}
    return value


def project_final_layers(index_path, output, resume=False, workers=8):
    """Publish L1-L5 graph/crop labels after fusion; preserve L6+ detail owners via adsorption.

    Fusion operations, source PNGs, observation quality and page-cut labels stay unchanged.
    L6+ boxes are absorbed into large owners, then owners already covered by L5 are skipped;
    only remaining owners are projected to L5. Later detail-filter and packing semantics
    are unchanged.
    """
    index_path, output = Path(index_path).resolve(), Path(output).resolve()
    source = json.loads(index_path.read_text())
    if source.get('schema') != 'gui_component_dataset_index_v1' or source.get('publication_status') != 'validated':
        raise ValueError('A validated component index is required')
    if source.get('final_layer_projection'):
        raise ValueError('Input already has a final layer projection')
    for root in (index_path.parent, Path(source['source_root']).resolve()):
        if root==output or root in output.parents or output in root.parents:
            raise ValueError('Layer projection must use an independent sibling directory')
    if not 1 <= workers <= 32:
        raise ValueError('workers must be 1..32')
    for artifact in source['artifacts'].values():
        if sha256_file(artifact['path']) != artifact['sha256']:
            raise ValueError('Changed source artifact: '+artifact['path'])
    if not json.loads(Path(source['artifacts']['validation']['path']).read_text()).get('passed'):
        raise ValueError('Source validation failed')
    policy = {'rule_version':RULE,'max_layers':5,'max_derived_depth':4,'root_layer':0,
              'basis':'actual_parent_depth_after_fusion_with_l6_l5_backfill', 'promote_deep_nodes':True,
              'l6_detail_promotion':dict(PROMOTION_POLICY),
              'source_index_sha256':sha256_file(index_path)}
    final_path = output/'index-final.json'
    if output.exists() and any(output.iterdir()):
        if resume and final_path.is_file():
            previous = json.loads(final_path.read_text())
            if previous.get('final_layer_projection') == policy:
                for artifact in previous['artifacts'].values():
                    if sha256_file(artifact['path']) != artifact['sha256']:
                        raise ValueError('Layer projection artifact changed')
                for observation in previous['observations']:
                    if observation.get('hierarchy') and sha256_file(observation['hierarchy']) != observation['hierarchy_sha256']:
                        raise ValueError('Projected graph changed')
                return {**json.loads((output/'reports/layer-projection.json').read_text()),'resumed':True,'final_index':str(final_path)}
        raise ValueError('Layer output must be empty or matching --resume output')
    manifest = source['artifacts'].get('detail_crop_manifest',source['artifacts']['detection_crops'])
    by = defaultdict(list)
    for crop in read_jsonl(manifest['path']):by[crop['observation_id']].append(crop)

    def project_one(observation):
        oid = observation['observation_id']
        audit = json.loads(Path(observation['audit']).read_text())
        folder = output/'observations'/oid
        result = deepcopy(audit)
        counts = Counter()
        selected = []
        record = dict(observation)
        if observation.get('hierarchy'):
            if sha256_file(observation['hierarchy']) != observation['hierarchy_sha256']:
                raise ValueError('Source hierarchy changed: '+oid)
            graph = json.loads(Path(observation['hierarchy']).read_text())
            for node in graph['nodes']:
                correct_pseudo_icon_label(node)
            graph, projected = truncate_graph(graph, promotion=PROMOTION_POLICY)
            graph['final_layer_projection'] = policy
            if observation['status']=='accepted':counts.update(projected)
            original_crops = result['crops']
            result['crops'] = [truncate_crop(c,graph) for c in original_crops]
            graph_path = folder/'hierarchy.json'
            for c in result['crops']:c['hierarchy']=str(graph_path)
            write_json(graph_path,graph)
            result['artifact_sha256'] = sha256_file(graph_path)
            result['final_layer_projection'] = policy
            record.update(hierarchy=str(graph_path),hierarchy_sha256=result['artifact_sha256'])
            if observation['status']=='accepted':
                selected = [truncate_crop(c,graph) for c in by[oid]]
                for c in selected:c['hierarchy']=str(graph_path)
                counts['crop_target_occurrences_removed'] += sum(len(c['targets']) for c in by[oid])-sum(len(c['targets']) for c in selected)
                counts['promoted_target_occurrences'] += sum(c['final_layer_projection']['promoted_target_occurrences'] for c in selected)
                record.update(published_crops=len(selected),detection_eligible=any(c['targets'] for c in selected))
            # Promoted owners are reattached to L4 ancestors, so the published tree must stay <= L5.
            depths = graph_depths(graph['nodes'])
            if any(d>=5 for d in depths.values()):raise ValueError('Deep node leaked after projection')
            identifiers = {n['global_node_id'] for n in graph['nodes']}
            for c in result['crops']:
                for t in c['targets']:
                    if t['global_node_id'] not in identifiers or t.get('global_parent_id') and t['global_parent_id'] not in identifiers:
                        raise ValueError('Dangling final target reference')
        write_json(folder/'result.json',result)
        record['audit']=str(folder/'result.json')
        record['rule_version']=GRAPH_RULE
        for key in ('detail_crop_candidates','discarded_detail_crops'):record.pop(key,None)
        return record,selected,counts

    observations,rows,counts = [],[],Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for observation,crops,metrics in pool.map(project_one,source['observations']):
            observations.append(observation);rows.extend(crops);counts.update(metrics)
    write_jsonl(output/'crops/trainable.jsonl',rows)
    write_jsonl(output/'quality/index.jsonl',(json.loads(Path(o['audit']).read_text())['quality'] for o in observations))
    report = {'schema':RULE,'policy':policy,'observations':len(observations),'counts':dict(counts),
              'candidate_crops':len(rows),'quality_statuses_unchanged':True,'split_labels_unchanged':True,
              'fusion_and_cut_coordinates_unchanged':True,'passed':True}
    write_json(output/'reports/layer-projection.json',report)
    write_json(output/'reports/validation.json',{'passed':True,'scope':'final_layer_projection_l6_backfill',
               'source_validation':source['artifacts']['validation'],'observations_checked':len(observations),
               'max_derived_depth':4,'l6_detail_promotion':dict(PROMOTION_POLICY),'dangling_positive_references':0})
    index = {**source,'rule_version':GRAPH_RULE,'observations':observations,
             'final_layer_projection':policy,'parent':{'path':str(index_path),'sha256':policy['source_index_sha256']},
             'artifacts':dict(source['artifacts'])}
    index.pop('detail_training_selection',None)
    for key in ('detail_crop_manifest','discarded_detail_crops','detail_selection_report'):
        index['artifacts'].pop(key,None)
    index['artifacts']['source_generation_validation']=source['artifacts']['validation']
    for key,name in [('detection_crops','crops/trainable.jsonl'),('quality','quality/index.jsonl'),
                     ('layer_projection_report','reports/layer-projection.json'),('validation','reports/validation.json')]:
        path=output/name;index['artifacts'][key]={'path':str(path),'sha256':sha256_file(path)}
    write_json(final_path,index)
    return {**report,'final_index':str(final_path)}
