"""Recomputable dataset statistics with explicit denominators, streaming JSONL and bounded pair matrices."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import csv
import json
import math
import numpy as np
from .infrastructure import read_jsonl, write_json


def distribution(values, edges):
    """Count scalar values in half-open buckets; the last bucket extends to infinity."""
    counts = Counter()
    for value in values:
        index = int(np.searchsorted(edges, value, side='right'))
        label = f'<{edges[0]}' if index == 0 else f'>={edges[-1]}' if index == len(edges) else f'[{edges[index-1]},{edges[index]})'
        counts[label] += 1
    return dict(counts)


def width_bucket(width, edges):
    """Assign original CSS width to inclusive upper-bound training buckets, with explicit overflow."""
    return next((str(edge) for edge in edges if width <= edge), f'>{edges[-1]}')


def iou_statistics(boxes, edges):
    """Count all unordered within-sample pairs and per-box maximum IoU using <=128 by N blocks.

    Zero IoU is its own bucket. Pair ratios use N*(N-1)/2; per-box maximum ratios use
    N for samples with N>=2. Singletons have no comparable neighbor and are excluded.
    """
    boxes = np.asarray(boxes, dtype=np.float64).reshape(-1, 4)
    count = len(boxes)
    names = ['zero'] + [f'({edges[i]},{edges[i+1]})' if i == 0 else f'[{edges[i]},{edges[i+1]})' for i in range(len(edges)-1)]
    names[-1] = names[-1][:-1] + ']'
    pair_hist = np.zeros(len(names), dtype=np.int64)
    max_hist = np.zeros(len(names), dtype=np.int64)
    if count < 2:
        return {'pair_count': 0, 'nonzero_pairs': 0, 'pair_bins': dict(zip(names, map(int, pair_hist))),
                'comparable_boxes': 0, 'singleton_boxes': count, 'max_iou_bins': dict(zip(names, map(int, max_hist)))}
    areas = np.maximum(0, boxes[:, 2]-boxes[:, 0])*np.maximum(0, boxes[:, 3]-boxes[:, 1])
    maxima = np.zeros(count)
    nonzero = 0
    for start in range(0, count, 128):
        end = min(count, start+128)
        a = boxes[start:end]
        iw = np.maximum(0, np.minimum(a[:, None, 2], boxes[None, :, 2])-np.maximum(a[:, None, 0], boxes[None, :, 0]))
        ih = np.maximum(0, np.minimum(a[:, None, 3], boxes[None, :, 3])-np.maximum(a[:, None, 1], boxes[None, :, 1]))
        inter = iw*ih
        values = inter / np.maximum(areas[start:end, None]+areas[None, :]-inter, 1e-12)
        values[np.arange(end-start), np.arange(start, end)] = 0
        maxima[start:end] = values.max(axis=1)
        selected = values[np.arange(count)[None, :] > np.arange(start, end)[:, None]]
        nonzero += int(np.count_nonzero(selected))
        indices = np.minimum(len(edges)-2, np.maximum(0, np.searchsorted(edges, selected, side='right')-1))+1
        indices[selected == 0] = 0
        pair_hist += np.bincount(indices, minlength=len(names))
    indices = np.minimum(len(edges)-2, np.maximum(0, np.searchsorted(edges, maxima, side='right')-1))+1
    indices[maxima == 0] = 0
    max_hist += np.bincount(indices, minlength=len(names))
    return {'pair_count': count*(count-1)//2, 'nonzero_pairs': nonzero,
            'pair_bins': dict(zip(names, map(int, pair_hist))), 'comparable_boxes': count,
            'singleton_boxes': 0, 'max_iou_bins': dict(zip(names, map(int, max_hist)))}


def compute_statistics(root, workers=12):
    """Recompute aggregate and width-bucket reports from published output; never mutate source datasets."""
    root = Path(root).resolve()
    run = json.loads((root/'run.json').read_text())
    config = run['config']
    widths = config['statistics']['width_buckets']
    edges = config['statistics']['iou_edges']
    totals, reasons, source_versions, statuses = Counter(), Counter(), Counter(), Counter()
    page_statuses = defaultdict(set)
    cleanup_reasons, source_classes, final_classes = Counter(), Counter(), Counter()
    heights, crop_heights, crop_counts, cut_counts, target_counts = [], [], [], [], []
    rows = []
    quality = list(read_jsonl(root/'quality'/'index.jsonl'))
    for q in quality:
        result = json.loads((root/'observations'/q['observation_id']/'result.json').read_text())
        c = result['counts']
        statuses[q['status']] += 1
        page_statuses[q['page_id']].add(q['status'])
        reasons.update(q['reason_codes'])
        source_versions[q.get('source_rule_version', 'unknown')] += 1
        totals['source_nodes'] += c.get('source_nodes', 0)
        totals['source_detection_boxes'] += c.get('source_detection', 0)
        size = q.get('original_size', {})
        if 'height' in size:
            heights.append(size['height'])
        if q['status'] != 'accepted':
            totals[f"source_boxes_withheld_{q['status']}"] += c.get('source_detection', 0)
        else:
            totals['source_detection_boxes_on_accepted_observations'] += c.get('source_detection', 0)
            totals['cleaned_global_detection_boxes'] += c.get('final_detection', 0)
            totals['generated_groups'] += c.get('generated_groups', 0)
            totals['activated_source_groups'] += c.get('activated_source_groups', 0)
            cleanup_reasons.update(c.get('excluded_by_reason', {}))
            graph = json.loads((root/'observations'/q['observation_id']/'hierarchy.json').read_text())
            totals['active_generated_groups'] += sum(bool(n.get('generated')) and n['detection_eligible'] for n in graph['nodes'])
            totals['excluded_source_detection_boxes'] += sum(n.get('source_detection_eligible', False) and not n['detection_eligible'] for n in graph['nodes'])
            source_classes.update(n.get('struct', 'unknown') for n in graph['nodes'] if n.get('source_detection_eligible'))
            final_classes.update(n.get('struct', 'unknown') for n in graph['nodes'] if n['detection_eligible'])
            totals['important_source_targets'] += q.get('evidence', {}).get('cleanup', {}).get('important_source_targets', 0)
            totals['important_exclusion_candidates'] += len(q.get('evidence', {}).get('cleanup', {}).get('excluded_important_ids', []))
            totals['targets_without_complete_crop'] += len(q.get('evidence', {}).get('unrepresented_target_ids', []))
        label = result.get('split_label')
        cuts = len(label['cut_lines_original_px']) if label else None
        if label:
            cut_counts.append(cuts)
            totals['cut_lines'] += cuts
            totals['split_observations'] += bool(cuts)
            totals['whole_image_observations'] += not cuts
            totals['split_label_candidates'] += 1
            totals['trainable_split_labels'] += label['supervision_eligible']
            if label['supervision_eligible']:
                totals['trainable_cut_lines'] += cuts
                totals['trainable_split_observations'] += bool(cuts)
                totals['trainable_no_split_observations'] += not cuts
            totals['fallback_observations'] += label['fallback']
            totals['cut_object_intersections'] += label['cut_object_count']
        crop_counts.append(len(result['crops']))
        rows.append({'observation_id': q['observation_id'], 'page_id': q['page_id'], 'viewport': q['viewport'],
                     'status': q['status'], 'reasons': '|'.join(q['reason_codes']),
                     'width': size.get('width'), 'height': size.get('height'),
                     'source_boxes': c.get('source_detection', 0), 'cleaned_boxes': c.get('final_detection', 0),
                     'generated_groups': c.get('generated_groups', 0), 'cuts': cuts, 'crops': len(result['crops']),
                     'trainable_crops': sum(x['status'] == 'accepted' for x in result['crops']),
                     'split_supervision_eligible': bool(label and label['supervision_eligible'])})
    buckets = {}
    published_ids = set()
    crop_rows = []
    def inspect_crop(crop):
        boxes = [n['bbox'] for n in crop['targets']]
        return crop, iou_statistics(boxes, edges)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # executor.map would eagerly consume an unbounded manifest on older Python;
        # process finite batches instead.
        iterator = iter(read_jsonl(root/'crops'/'trainable.jsonl'))
        while True:
            batch = []
            for _ in range(workers*4):
                try:
                    batch.append(next(iterator))
                except StopIteration:
                    break
            if not batch:
                break
            for crop, iou in pool.map(inspect_crop, batch):
                box = crop['crop_bbox']
                width, height = box[2]-box[0], box[3]-box[1]
                bucket = width_bucket(width, widths)
                record = buckets.setdefault(bucket, {'crops': 0, 'boxes': 0, 'ignore_regions': 0,
                                                       'pair_count': 0, 'nonzero_pairs': 0, 'comparable_boxes': 0,
                                                       'singleton_boxes': 0, 'pair_bins': Counter(), 'max_iou_bins': Counter(),
                                                       'heights': [], 'target_counts': []})
                record['crops'] += 1
                record['boxes'] += len(crop['targets'])
                record['ignore_regions'] += len(crop['ignore_regions'])
                record['heights'].append(height)
                record['target_counts'].append(len(crop['targets']))
                for key in ('pair_count', 'nonzero_pairs', 'comparable_boxes', 'singleton_boxes'):
                    record[key] += iou[key]
                record['pair_bins'].update(iou['pair_bins'])
                record['max_iou_bins'].update(iou['max_iou_bins'])
                published_ids.update(n['global_node_id'] for n in crop['targets'])
                totals['trainable_crops'] += 1
                totals['crop_target_occurrences'] += len(crop['targets'])
                totals['fragment_ignores'] += sum(n.get('reason') == 'boundary_fragment' for n in crop['ignore_regions'])
                totals['source_ignores'] += sum(n.get('reason') == 'source_ignore' for n in crop['ignore_regions'])
                crop_heights.append(height)
                target_counts.append(len(crop['targets']))
                crop_rows.append({'crop_id': crop['crop_id'], 'width_bucket': bucket, 'width': width, 'height': height,
                                  'boxes': len(crop['targets']), 'ignore_regions': len(crop['ignore_regions']),
                                  'pair_count': iou['pair_count'], 'nonzero_pairs': iou['nonzero_pairs']})
    totals['published_unique_detection_boxes'] = len(published_ids)
    totals['context_duplicate_occurrences'] = totals['crop_target_occurrences']-len(published_ids)
    totals['withheld_crop_candidates'] = sum(r['crops']-r['trainable_crops'] for r in rows)
    reconciled = (totals['source_detection_boxes_on_accepted_observations'] - totals['excluded_source_detection_boxes']
                  + totals['active_generated_groups'] + totals['activated_source_groups'])
    if reconciled != totals['cleaned_global_detection_boxes']:
        raise ValueError('Source/derived detection count reconciliation failed')
    for record in buckets.values():
        for key, denominator in (('pair_bins', record['pair_count']), ('max_iou_bins', record['comparable_boxes'])):
            record[key] = {name: {'count': int(value), 'ratio': value/denominator if denominator else 0}
                           for name, value in record[key].items()}
        record['nonzero_pair_ratio'] = record['nonzero_pairs']/record['pair_count'] if record['pair_count'] else 0
        record['height_distribution'] = distribution(record.pop('heights'), [900, 1200, 1500, 1801, 2701])
        record['boxes_per_crop'] = distribution(record.pop('target_counts'), [1, 10, 50, 100, 200, 300, 385])
    summary = {'schema': 'gui_postprocess_statistics_v1', 'rule_version': run['rule_version'],
               'config_fingerprint': run['config_fingerprint'], 'pages': len(page_statuses), 'observations': len(quality),
               'observation_statuses': dict(statuses),
               'pages_with_any_accepted_viewport': sum('accepted' in states for states in page_statuses.values()),
               'pages_with_no_accepted_viewport': sum('accepted' not in states for states in page_statuses.values()),
               'source_versions': dict(source_versions), 'totals': dict(totals),
               'quality_reasons': dict(reasons), 'cleanup_exclusion_reasons': dict(cleanup_reasons),
               'cleanup_exclusion_ratios': {reason: count/totals['source_detection_boxes_on_accepted_observations']
                                            if totals['source_detection_boxes_on_accepted_observations'] else 0
                                            for reason, count in cleanup_reasons.items()},
               'source_classes_on_accepted_observations': dict(source_classes), 'cleaned_detection_classes': dict(final_classes),
               'input_height_distribution': distribution(heights, [900, 1801, 2701, 5000, 8000]),
               'crop_height_distribution': distribution(crop_heights, [900, 1200, 1500, 1801, 2701]),
               'crops_per_observation': dict(Counter(crop_counts)), 'cuts_per_processed_observation': dict(Counter(cut_counts)),
               'boxes_per_crop': distribution(target_counts, [1, 10, 50, 100, 200, 300, 385]),
               'width_buckets': buckets,
               'definitions': {'iou': 'Label geometry overlap within the same trainable crop; not detector prediction IoU or AP.',
                               'pair_ratio_denominator': 'All unordered pairs within crops in this width bucket, including zero overlap.',
                               'max_iou_ratio_denominator': 'All boxes in crops with >=2 boxes; singletons reported separately.',
                               'width_bucket': 'Original CSS width, assigned to first inclusive configured upper bound.',
                               'cleaned_global_detection_boxes': 'Unique objects on accepted observations before crop publication.',
                               'crop_target_occurrences': 'Complete targets in trainable crops, including context duplicates.',
                               'important_exclusion_candidates': 'Audit candidates, not manually verified erroneous deletions.',
                               'semantic_quality': 'Rules and known suspects only; no OCR/template corpus, no verified detection ground truth.'}}
    reports = root/'reports'
    write_json(reports/'summary.json', summary)
    def save_csv(path, values):
        if not values:
            return
        with path.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(values[0]))
            writer.writeheader()
            writer.writerows(values)
    save_csv(reports/'observations.csv', rows)
    save_csv(reports/'crops.csv', crop_rows)
    iou_rows = []
    for bucket, record in buckets.items():
        for metric in ('pair_bins', 'max_iou_bins'):
            for interval, value in record[metric].items():
                iou_rows.append({'width_bucket': bucket, 'metric': metric, 'interval': interval, **value})
    save_csv(reports/'iou-buckets.csv', iou_rows)
    lines = ['# 后处理统计', '', f"页面：{len(page_statuses)}；视口：{len(quality)}；状态：{dict(statuses)}", '',
             '| 指标 | 数量 |', '|---|---:|']
    lines.extend(f'| {key} | {value} |' for key, value in totals.items())
    lines.extend(['', '## 抛弃与待审', '', f'质量原因：`{dict(reasons)}`', '', f'框退出监督原因：`{dict(cleanup_reasons)}`', '',
                  '## 宽度分桶', '', '| 宽度上限 | 裁片 | 框出现次数 | 非零 IoU 框对 | 全部框对 | 非零比例 |', '|---|---:|---:|---:|---:|---:|'])
    lines.extend(f"| {bucket} | {r['crops']} | {r['boxes']} | {r['nonzero_pairs']} | {r['pair_count']} | {r['nonzero_pair_ratio']:.4%} |" for bucket, r in buckets.items())
    lines.extend(['', 'IoU 是同一裁片内标签框之间的几何重叠，不是检测准确率。完整分布见 summary.json 和 iou-buckets.csv。', '',
                  f'裁片高度分布：`{summary["crop_height_distribution"]}`', '',
                  '所有标签仍是规则生成候选；重要对象误删数未经人工确认，仅报告排除候选。'])
    (reports/'summary.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return summary
