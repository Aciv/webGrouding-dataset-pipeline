"""Layer counts for source candidates, current component graphs and selected detail-training crop views."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
from .domain import graph_depths
from .infrastructure import read_jsonl, sha256_file, write_json


def compute_layer_statistics(index_path, output=None, workers=8):
    """Summarize accepted viewport graphs by actual parent depth, with original layer kept separate.

    Counts of unique global IDs never merge objects across viewports. Crop occurrence
    counts include context repetition; coverage-discarded crops do not reject a page.
    Depth zero is displayed as L1. Final publication counts reflect its L1-L5 projection;
    raw source layers are independent audit evidence and may be deeper.
    """
    index_path = Path(index_path).resolve()
    index = json.loads(index_path.read_text())
    if index.get('schema') != 'gui_component_dataset_index_v1' or index.get('publication_status') != 'validated':
        raise ValueError('Validated component final index required')
    if not 1 <= workers <= 32:
        raise ValueError('workers must be 1..32')
    output = Path(output).resolve() if output else index_path.parent/'reports'
    observations = [o for o in index['observations'] if o['status']=='accepted']
    totals, original, all_source = Counter(), Counter(), Counter()
    current, classes, lookup = defaultdict(Counter), defaultdict(Counter), {}
    mismatches = 0

    def inspect(observation):
        if sha256_file(observation['hierarchy']) != observation['hierarchy_sha256']:
            raise ValueError('Changed hierarchy: '+observation['observation_id'])
        graph = json.loads(Path(observation['hierarchy']).read_text())
        depths = graph_depths(graph['nodes'])
        raw = json.loads(Path(observation['source_hierarchy']).read_text()).get('nodes',[]) if observation.get('source_hierarchy') else None
        return graph['nodes'], depths, raw

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for nodes, depths, raw in pool.map(inspect, observations):
            source_nodes = raw if raw is not None else [n for n in nodes if not n.get('generated')]
            for node in source_nodes:
                source_layer = node.get('layer') if raw is not None else node.get('source_layer')
                source_layer = str(source_layer) if type(source_layer) is int and source_layer>=0 else 'unknown'
                all_source[source_layer] += 1
                detectable = node.get('disposition')=='detection' if raw is not None else node.get('source_detection_eligible')
                if detectable:original[source_layer] += 1
            for node in nodes:
                depth = depths[node['node_id']]
                current[depth]['graph_nodes'] += 1
                mismatches += node.get('derived_depth') != depth
                if node.get('detection_eligible'):
                    current[depth]['global_detection_targets'] += 1
                    current[depth]['generated_detection_targets'] += bool(node.get('generated'))
                    classes[depth][node.get('struct') or 'unknown'] += 1
                    identifier = node['global_node_id']
                    if identifier in lookup:
                        raise ValueError('Duplicate global object ID')
                    lookup[identifier] = depth
    candidate_ids, training_ids, discarded_ids = set(), set(), set()
    manifest = index['artifacts'].get('detail_crop_manifest', index['artifacts']['detection_crops'])
    if sha256_file(manifest['path']) != manifest['sha256']:
        raise ValueError('Changed crop manifest')
    for crop in read_jsonl(manifest['path']):
        eligible = crop.get('detail_training_eligible',True)
        totals['candidate_crop_views'] += 1
        totals['detail_trainable_crop_views' if eligible else 'detail_discarded_crop_views'] += 1
        for target in crop['targets']:
            identifier = target['global_node_id']
            if identifier not in lookup:
                raise ValueError('Crop target missing in accepted component graph: '+identifier)
            depth = lookup[identifier]
            candidate_ids.add(identifier)
            current[depth]['candidate_crop_occurrences'] += 1
            if eligible:
                training_ids.add(identifier)
                current[depth]['detail_training_occurrences'] += 1
            else:
                discarded_ids.add(identifier)
                current[depth]['detail_discarded_occurrences'] += 1
    for identifier in candidate_ids:current[lookup[identifier]]['candidate_unique_targets'] += 1
    for identifier in training_ids:current[lookup[identifier]]['detail_training_unique_targets'] += 1
    for identifier in candidate_ids-training_ids:current[lookup[identifier]]['withheld_only_by_detail_selection'] += 1
    for identifier in lookup.keys()-candidate_ids:current[lookup[identifier]]['targets_without_complete_candidate_crop'] += 1
    fields = ['graph_nodes','global_detection_targets','generated_detection_targets','candidate_unique_targets',
              'candidate_crop_occurrences','detail_training_unique_targets','detail_training_occurrences',
              'detail_discarded_occurrences','withheld_only_by_detail_selection','targets_without_complete_candidate_crop']
    rows = [{'layer':f'L{depth+1}', 'derived_depth':depth,
             **{key:current[depth][key] for key in fields}} for depth in sorted(current)]
    original_rows = [{'source_layer':layer, 'display_layer':'unknown' if layer=='unknown' else f'L{int(layer)+1}',
                      'source_nodes':all_source[layer], 'source_detection_candidates':original[layer]}
                     for layer in sorted(all_source,key=lambda x:(x=='unknown',int(x) if x!='unknown' else 0))]
    report = {'schema':'gui_layer_statistics_v1', 'rule_version':index['rule_version'],
              'source_index_sha256':sha256_file(index_path), 'accepted_observations':len(observations),
              'scope':'Accepted viewports; global object IDs remain viewport-specific',
              'layer_definition':('L1=derived_depth 0; L5 includes true depth-4 nodes and selected L6+ adsorption owners'
                                  if index.get('final_layer_projection') else
                                  'L1=derived_depth 0; intermediate full parent depth, not the final L5 export'),
              'source_layer_definition':'Original source_layer, preserved separately; unknown is not inferred',
              'count_definition':'Unique targets deduplicate crop context within a viewport; occurrences do not',
              'derived_depth_mismatches':mismatches, 'layers':rows, 'source_layers':original_rows,
              'final_layer_projection':index.get('final_layer_projection'),
              'classes_by_derived_depth':{str(d):dict(c) for d,c in sorted(classes.items())},
              'totals':{**dict(totals), **{key:sum(row[key] for row in rows) for key in fields}}}
    if mismatches:
        raise ValueError('Stored derived_depth differs from actual parent depth')
    write_json(output/'layer-statistics.json',report)
    output.mkdir(parents=True,exist_ok=True)
    with (output/'layer-statistics.csv').open('w',encoding='utf-8',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=['layer','derived_depth',*fields]);writer.writeheader();writer.writerows(rows)
    with (output/'source-layer-statistics.csv').open('w',encoding='utf-8',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=['source_layer','display_layer','source_nodes','source_detection_candidates']);writer.writeheader();writer.writerows(original_rows)
    lines = ['# 每层框统计','',
             ('最终层L1-L5：L1=derived_depth 0；L5包含真实depth-4节点与L6+吸附补入owner。原始source_layer另列，可能更深。'
              if index.get('final_layer_projection') else
              '本表是未截断的中间完整图，不是最终L5成品；L1=derived_depth 0，原始source_layer单独列出。'),
             '仅统计质量通过的视口；不同视口的对象不做语义合并。去重框数按global_node_id去除裁片上下文重复；出现次数保留重复。','',
             '| 当前层 | 整页检测去重框 | 裁片候选去重框 | 细节训练去重框 | 细节训练框出现次数 | 仅因20%规则退出的去重框 |',
             '|---|---:|---:|---:|---:|---:|']
    for row in rows:
        lines.append('| '+ ' | '.join(str(row[k]) for k in ['layer','global_detection_targets','candidate_unique_targets',
                     'detail_training_unique_targets','detail_training_occurrences','withheld_only_by_detail_selection'])+' |')
    lines += ['| **合计** | '+' | '.join(str(report['totals'][k]) for k in ['global_detection_targets','candidate_unique_targets',
              'detail_training_unique_targets','detail_training_occurrences','withheld_only_by_detail_selection'])+' |','',
              '## 原始层级','', '| 原始source_layer | 展示层 | 原始检测候选框 |','|---|---|---:|']
    for row in original_rows:lines.append(f"| {row['source_layer']} | {row['display_layer']} | {row['source_detection_candidates']} |")
    lines += ['', '完整字段与逐层类别分布见layer-statistics.json；CSV可直接做表格分析。',
              '低覆盖退出框若仍出现在其他保留裁片中，不计入“仅因20%规则退出的去重框”。结构祖先保留不代表它本身可用于检测训练。']
    (output/'layer-statistics.md').write_text('\n'.join(lines)+'\n')
    return report
