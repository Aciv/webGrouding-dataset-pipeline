"""Enforce the final L1-L5 publication boundary after existing fusion and slicing."""
from collections import Counter, defaultdict
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from scripts.offline_postprocess.domain import graph_depths
from scripts.offline_postprocess.infrastructure import read_jsonl, write_json, write_jsonl, sha256_file

RULE = 'final_component_layers_v1'


def truncate_graph(graph, max_layers=5):
    """Keep true parent depths 0..max_layers-1 without promoting, flattening or restoring fused nodes."""
    if type(max_layers) is not int or not 1 <= max_layers <= 5:
        raise ValueError('Final graph must retain between one and five layers')
    depths = graph_depths(graph['nodes'])
    kept = [deepcopy(n) for n in graph['nodes'] if depths[n['node_id']] < max_layers]
    identifiers = {n['global_node_id'] for n in kept}
    for node in kept:
        node['layer'] = node['derived_depth'] = depths[node['node_id']]
        if node.get('represented_by') and node['represented_by'] not in identifiers:
            node.pop('represented_by')
    result = {**graph, 'nodes':kept, 'detection_target_ids':[n['global_node_id'] for n in kept if n.get('detection_eligible')]}
    result['rule_version'] = 'component_slices_v4_final_l5'
    counts = {'nodes_before':len(graph['nodes']), 'nodes_after':len(kept),
              'detection_targets_before':sum(bool(n.get('detection_eligible')) for n in graph['nodes']),
              'detection_targets_after':len(result['detection_target_ids'])}
    return result, counts


def truncate_crop(crop, graph):
    """Remove deep positive/ignore references from crop-local xyxy labels; keep crop/core coordinates fixed."""
    nodes = {n['global_node_id']:n for n in graph['nodes']}
    value = deepcopy(crop)
    value['targets'] = [{**t, 'layer':nodes[t['global_node_id']]['layer'],
                         'derived_depth':nodes[t['global_node_id']]['derived_depth']}
                        for t in crop['targets'] if t['global_node_id'] in nodes and nodes[t['global_node_id']].get('detection_eligible')]
    value['ignore_regions'] = [i for i in value['ignore_regions']
                              if (not i.get('global_node_id') or i['global_node_id'] in nodes)
                              and (not i.get('global_parent_id') or i['global_parent_id'] in nodes)]
    for key in ('target_union_coverage','detail_training_eligible','detail_training_discarded','detail_training_reason_codes'):
        value.pop(key,None)
    value['final_layer_projection'] = {'max_layers':5, 'discarded_target_occurrences':len(crop['targets'])-len(value['targets'])}
    return value


def project_final_layers(index_path, output, resume=False, workers=8):
    """Publish shallow graph/crop references from immutable validated generation evidence, then reselect coverage.

    Fusion operations, source PNGs, observation quality and page-cut labels stay unchanged.
    Deeper nodes remain only in the original audit run, never the final graph/positive labels.
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
              'basis':'actual_parent_depth_after_fusion', 'promote_deep_nodes':False,
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
            graph, projected = truncate_graph(graph)
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
                record.update(published_crops=len(selected),detection_eligible=any(c['targets'] for c in selected))
            # Projection validity is independent of old full-tree slicing-loss diagnostics.
            depths = graph_depths(graph['nodes'])
            if any(d>=5 for d in depths.values()):raise ValueError('Deep node leaked after projection')
            identifiers = {n['global_node_id'] for n in graph['nodes']}
            for c in result['crops']:
                for t in c['targets']:
                    if t['global_node_id'] not in identifiers or t.get('global_parent_id') and t['global_parent_id'] not in identifiers:
                        raise ValueError('Dangling final target reference')
        write_json(folder/'result.json',result)
        record['audit']=str(folder/'result.json')
        record['rule_version']='component_slices_v4_final_l5'
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
    write_json(output/'reports/validation.json',{'passed':True,'scope':'final_layer_projection',
               'source_validation':source['artifacts']['validation'],'observations_checked':len(observations),
               'max_derived_depth':4,'dangling_positive_references':0})
    index = {**source,'rule_version':'component_slices_v4_final_l5','observations':observations,
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
