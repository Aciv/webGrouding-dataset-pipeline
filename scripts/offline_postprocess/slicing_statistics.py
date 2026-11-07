"""Independent post-slice diagnostics, exact ignore union, boundary recovery and version comparisons."""
from collections import Counter
from pathlib import Path
import csv
import json
import numpy as np
from .cleanup import critical_target
from .domain import area, intersection, contains
from .infrastructure import read_jsonl, write_json


def rectangle_union_area(boxes):
    """Compute exact union area of CSS xyxy rectangles without double-counting nested ignores."""
    boxes = [b for b in boxes if area(b) > 0]
    if not boxes:
        return 0.0
    ys = sorted({b[1] for b in boxes} | {b[3] for b in boxes})
    result = 0.0
    for top, bottom in zip(ys, ys[1:]):
        intervals = sorted((b[0], b[2]) for b in boxes if b[1] < bottom and b[3] > top)
        if not intervals:
            continue
        start, end = intervals[0]
        length = 0.0
        for left, right in intervals[1:]:
            if left > end:
                length += end-start
                start, end = left, right
            else:
                end = max(end, right)
        result += (length+end-start)*(bottom-top)
    return result


def describe(values):
    """Return scalar count, extrema, mean and percentiles; empty populations stay explicit."""
    if not values:
        return {'count': 0, 'min': None, 'max': None, 'mean': None, 'p50': None, 'p90': None, 'p99': None}
    a = np.array(values, dtype=float)
    return {'count': len(values), 'min': float(a.min()), 'max': float(a.max()), 'mean': float(a.mean()),
            **{f'p{p}': float(np.percentile(a, p)) for p in [50, 90, 99]}}


def compute_slicing_statistics(root, baseline=None):
    """Audit all candidate partitions and published crop supervision, reporting geometry separately from visual risks."""
    root = Path(root).resolve()
    run = json.loads((root/'run.json').read_text())
    policy = run['config']['slicing']
    counts, violations, tiers, reasons = Counter(), Counter(), Counter(), Counter()
    critical_loss_types, component_loss_types = Counter(), Counter()
    values = {name: [] for name in ['core_height', 'crop_height', 'adjacent_overlap_px', 'context_overhead_ratio',
              'ignore_union_ratio', 'targets_per_crop', 'target_density_per_megapixel', 'aspect_ratio',
              'cut_clearance_px', 'cut_spacing_px', 'crops_per_observation',
              'long_core_height', 'long_crop_height', 'whole_image_height', 'local_region_height']}
    observation_rows, crop_rows, cut_rows, problem_rows = [], [], [], []
    for q in read_jsonl(root/'quality'/'index.jsonl'):
        folder = root/'observations'/q['observation_id']
        result = json.loads((folder/'result.json').read_text())
        if not result.get('split_label') or not (folder/'hierarchy.json').exists() or q['status'] != 'accepted':
            continue
        label = result['split_label']
        graph = json.loads((folder/'hierarchy.json').read_text())
        nodes = [n for n in graph['nodes'] if n['detection_eligible']]
        index = {n['global_node_id']: n for n in nodes}
        all_crops = result['crops']
        published = [c for c in all_crops if c['status'] == 'accepted']
        full = {n['global_node_id'] for c in published for n in c['targets']}
        missing = set(index)-full
        critical_missing = sorted(key for key in missing if critical_target(index[key]))
        component_missing = sorted(missing-set(critical_missing))
        critical_loss_types.update(f"{index[key]['struct']}/{index[key].get('subtype')}" for key in critical_missing)
        component_loss_types.update(f"{index[key]['struct']}/{index[key].get('subtype')}" for key in component_missing)
        width, height = q['original_size']['width'], q['original_size']['height']
        cuts = label['cut_lines_original_px']
        counts['partitioned_observations'] += 1
        counts['long_observations'] += bool(cuts)
        counts['whole_image_observations'] += not cuts
        if not cuts:
            values['whole_image_height'].append(height)
        counts['candidate_crops'] += len(all_crops)
        counts['published_crops'] += len(published)
        counts['omitted_crops'] += len(all_crops)-len(published)
        counts['global_targets'] += len(nodes)
        counts['critical_targets'] += sum(critical_target(n) for n in nodes)
        counts['critical_targets_without_complete_view'] += len(critical_missing)
        counts['critical_loss_with_source_overflow'] += sum(not contains([0, 0, width, height], index[key]['bbox']) for key in critical_missing)
        counts['critical_loss_inside_source_bounds'] += sum(contains([0, 0, width, height], index[key]['bbox']) for key in critical_missing)
        counts['component_targets_without_complete_view'] += len(component_missing)
        counts['observations_with_critical_loss'] += bool(critical_missing)
        counts['observations_with_component_only_loss'] += bool(component_missing and not critical_missing)
        counts['split_supervision_eligible'] += label['supervision_eligible']
        counts['height_fallback_observations'] += label['fallback']
        tiers[label.get('quality_tier', 'legacy_ungraded')] += 1
        reasons.update(label.get('supervision_reason_codes', []))
        attempts = result['partition'].get('critical_replan_attempts', [])
        counts['critical_replan_observations'] += len(attempts) > 1
        if attempts:
            counts['critical_targets_recovered_by_replanning'] += max(0, attempts[0]['critical_missing']-len(critical_missing))
        counts['local_region_proposals'] += len(result['partition'].get('local_region_proposals', []))
        counts['applied_local_partitions'] += sum(o['op'] == 'apply_local_partition' for o in result['operations'])
        counts['local_content_region_targets'] += sum(n.get('subtype') == 'pixel_content_region' for n in nodes)
        local_targets = [n for n in nodes if n.get('subtype') == 'pixel_content_region']
        values['local_region_height'].extend(n['bbox'][3]-n['bbox'][1] for n in local_targets)
        counts['local_region_targets_above_height_budget'] += sum(n['bbox'][3]-n['bbox'][1] > policy['max_height'] for n in local_targets)
        for operation in result['operations']:
            if operation['op'] == 'apply_local_partition':
                counts['local_horizontal_cuts'] += sum(c['axis'] == 'y' for c in operation['pixel_cuts'])
                counts['local_vertical_cuts'] += sum(c['axis'] == 'x' for c in operation['pixel_cuts'])
        values['crops_per_observation'].append(len(all_crops))
        boundaries = [0, *cuts, height]
        values['cut_spacing_px'].extend(b-a for a, b in zip(boundaries, boundaries[1:]))
        obs_errors = []
        if not all_crops or all_crops[0]['core_bbox'][1] != 0 or all_crops[-1]['core_bbox'][3] != height:
            obs_errors.append('incomplete_core_coverage')
        if any(a['core_bbox'][3] != b['core_bbox'][1] for a, b in zip(all_crops, all_crops[1:])):
            obs_errors.append('core_gap_or_overlap')
        if bool(cuts) != (height > policy['trigger_height_gt']):
            obs_errors.append('height_trigger_mismatch')
        total_crop_area = 0
        occurrences = 0
        for pos, crop in enumerate(all_crops):
            b, core = crop['crop_bbox'], crop['core_bbox']
            h, core_h = b[3]-b[1], core[3]-core[1]
            values['core_height'].append(core_h)
            values['crop_height'].append(h)
            if cuts:
                values['long_core_height'].append(core_h)
                values['long_crop_height'].append(h)
            total_crop_area += area(b)
            if not contains(b, core):
                obs_errors.append('core_outside_crop')
            if cuts and not policy['min_height'] <= h <= policy['max_height']:
                obs_errors.append('crop_height_violation')
            if cuts and not policy['min_height'] <= core_h <= policy['max_height']:
                obs_errors.append('core_height_violation')
            if pos:
                overlap = max(0, all_crops[pos-1]['crop_bbox'][3]-b[1])
                values['adjacent_overlap_px'].append(overlap)
                counts['adjacent_overlap_at_least_900px'] += overlap >= 900
            if crop['status'] != 'accepted':
                continue
            if len(crop['targets']) > policy['max_targets']:
                obs_errors.append('target_capacity_violation')
            occurrences += len(crop['targets'])
            ignores = [n['bbox'] for n in crop['ignore_regions']]
            ignore_ratio = rectangle_union_area(ignores)/max(1, area(b))
            overlap_positives = sum(any(area(intersection(n['bbox'], ig)) > 0 for ig in ignores) for n in crop['targets'])
            covered_positives = sum(any(contains(ig, n['bbox']) for ig in ignores) for n in crop['targets'])
            counts['positives_overlapping_ignore'] += overlap_positives
            counts['positives_inside_single_ignore'] += covered_positives
            counts['crops_ignore_union_above_half'] += ignore_ratio > .5
            counts['low_information_warning_crops'] += bool(crop.get('pixel_diagnostics', {}).get('low_information_warning'))
            counts['fragment_ignore_occurrences'] += sum(ig.get('reason') == 'boundary_fragment' for ig in crop['ignore_regions'])
            counts['positive_occurrences'] += len(crop['targets'])
            values['ignore_union_ratio'].append(ignore_ratio)
            values['targets_per_crop'].append(len(crop['targets']))
            values['target_density_per_megapixel'].append(len(crop['targets'])/max(1, area(b))*1e6)
            values['aspect_ratio'].append(h/width)
            crop_rows.append({'crop_id': crop['crop_id'], 'core_height': core_h, 'crop_height': h,
                              'targets': len(crop['targets']), 'ignore_regions': len(ignores),
                              'ignore_union_ratio': ignore_ratio, 'positive_ignore_overlap_count': overlap_positives,
                              'positive_inside_ignore_count': covered_positives,
                              'low_information_warning': crop.get('pixel_diagnostics', {}).get('low_information_warning', False)})
        overhead = total_crop_area/(width*height)-1
        values['context_overhead_ratio'].append(overhead)
        counts['high_context_overhead_observations'] += overhead > .5
        counts['original_image_pixels'] += width*height
        counts['candidate_crop_pixels'] += total_crop_area
        counts['context_duplicate_positive_occurrences'] += occurrences-len(full)
        crossing_ids, critical_crossing_ids = set(), set()
        for y in cuts:
            crossing = [n for n in nodes if n['bbox'][1]+.01 < y < n['bbox'][3]-.01]
            critical = [n for n in crossing if critical_target(n)]
            crossing_ids.update(n['global_node_id'] for n in crossing)
            critical_crossing_ids.update(n['global_node_id'] for n in critical)
            clearance = 0 if crossing else min((abs(y-edge) for n in nodes for edge in (n['bbox'][1], n['bbox'][3])), default=height)
            values['cut_clearance_px'].append(clearance)
            counts['cut_lines'] += 1
            counts['cut_lines_crossing_any_target'] += bool(crossing)
            counts['cut_lines_crossing_critical_target'] += bool(critical)
            counts['cut_lines_clearance_below_4px'] += clearance < 4
            cut_rows.append({'observation_id': q['observation_id'], 'y': y, 'normalized_y': y/height,
                             'crossing_targets': len(crossing), 'crossing_critical_targets': len(critical),
                             'crossing_targets_without_complete_view': sum(n['global_node_id'] not in full for n in crossing),
                             'clearance_px': clearance, 'split_supervision_eligible': label['supervision_eligible']})
        counts['unique_targets_crossed_by_core_lines'] += len(crossing_ids)
        counts['crossed_targets_recovered_in_context'] += len(crossing_ids & full)
        counts['unique_critical_targets_crossed_by_core_lines'] += len(critical_crossing_ids)
        counts['crossed_critical_targets_recovered_in_context'] += len(critical_crossing_ids & full)
        violations.update(set(obs_errors))
        obs_row = {'observation_id': q['observation_id'], 'height': height, 'cut_lines': len(cuts),
                   'candidate_crops': len(all_crops), 'published_crops': len(published),
                   'critical_missing': len(critical_missing), 'component_missing': len(component_missing),
                   'context_overhead_ratio': overhead, 'fallback': label['fallback'],
                   'split_supervision_eligible': label['supervision_eligible'],
                   'quality_tier': label.get('quality_tier', 'legacy_ungraded'), 'geometry_errors': '|'.join(sorted(set(obs_errors)))}
        observation_rows.append(obs_row)
        if critical_missing or component_missing or obs_errors:
            problem_rows.append({**obs_row, 'critical_missing_ids': critical_missing, 'component_missing_ids': component_missing,
                                 'missing_types': dict(Counter(f"{index[key]['struct']}/{index[key].get('subtype')}" for key in missing))})
    summary = {'schema': 'gui_slicing_diagnostics_v2', 'rule_version': run['rule_version'],
               'config_fingerprint': run['config_fingerprint'], 'counts': dict(counts),
               'geometry_violations': dict(violations), 'quality_tiers': dict(tiers), 'withheld_split_reasons': dict(reasons),
               'critical_loss_types': dict(critical_loss_types), 'component_loss_types': dict(component_loss_types),
               'distributions': {key: describe(v) for key, v in values.items()},
               'definitions': {'critical': 'Existing text/control/foreground image/media/graphic and evidenced paragraph targets; not manually verified salience.',
                 'component_missing': 'Eligible container target has no complete view; bounded loss may be tolerated.',
                 'ignore_overlap': 'Geometric overlap, not necessarily invalid. Downstream loss must give matched positives priority over ignore.',
                 'low_information_warning': 'Grayscale proxy warning, not proof of blank page or invisible DOM.',
                 'counts_population': 'Accepted observations with candidate page partitions; crop overlap diagnostics use published detection crops.',
                 'context_overhead_ratio': 'Sum of candidate crop pixel areas divided by original image area minus one.'}}
    def ratio(num, den):
        return counts[num]/counts[den] if counts[den] else 0.0
    summary['rates'] = {'critical_target_loss_rate': ratio('critical_targets_without_complete_view', 'critical_targets'),
                        'pixel_weighted_context_overhead': counts['candidate_crop_pixels']/counts['original_image_pixels']-1 if counts['original_image_pixels'] else None,
                        'context_recovery_of_crossed_targets': ratio('crossed_targets_recovered_in_context', 'unique_targets_crossed_by_core_lines'),
                        'context_recovery_of_crossed_critical_targets': ratio('crossed_critical_targets_recovered_in_context', 'unique_critical_targets_crossed_by_core_lines')}
    def buckets(samples, boundaries):
        totals = Counter()
        for number in samples:
            position = int(np.searchsorted(boundaries, number, side='right'))
            name = f'<{boundaries[0]}' if position == 0 else f'>={boundaries[-1]}' if position == len(boundaries) else f'[{boundaries[position-1]},{boundaries[position]})'
            totals[name] += 1
        return dict(totals)
    summary['height_bins'] = {'long_actual_crops': buckets(values['long_crop_height'], [900, 1200, 1500, 1801]),
                             'whole_images': buckets(values['whole_image_height'], [900, 1801, 2701])}
    summary['overlap_bins_px'] = buckets(values['adjacent_overlap_px'], [1, 96, 192, 384, 900])
    summary['ignore_coverage_bins'] = buckets(values['ignore_union_ratio'], [.000000001, .1, .25, .5, 1.000000001])
    summary['crop_count_per_observation'] = dict(Counter(values['crops_per_observation']))
    if baseline:
        baseline = Path(baseline).resolve()
        if baseline == root:
            raise ValueError('Comparison baseline must differ from output')
        prior_path = baseline/'reports/slicing-summary.json'
        prior = json.loads(prior_path.read_text()) if prior_path.exists() else compute_slicing_statistics(baseline)
        summary['comparison'] = {'baseline': str(baseline), 'counts': {
            key: {'before': prior['counts'].get(key, 0), 'after': counts[key], 'delta': counts[key]-prior['counts'].get(key, 0)}
            for key in ['published_crops', 'cut_lines', 'critical_targets_without_complete_view',
                        'component_targets_without_complete_view', 'split_supervision_eligible',
                        'applied_local_partitions', 'context_duplicate_positive_occurrences']}}
        paired, exits = Counter(), Counter()
        for row in observation_rows:
            oid = row['observation_id']
            old_folder, new_folder = baseline/'observations'/oid, root/'observations'/oid
            if not (old_folder/'result.json').exists():
                continue
            old_result = json.loads((old_folder/'result.json').read_text())
            if old_result['quality']['status'] != 'accepted':
                continue
            new_result = json.loads((new_folder/'result.json').read_text())
            if old_result['quality'].get('source_hash') != new_result['quality'].get('source_hash'):
                continue
            old_graph = json.loads((old_folder/'hierarchy.json').read_text())
            new_graph = json.loads((new_folder/'hierarchy.json').read_text())
            # Generated ordinal IDs are not semantic identities across rule versions.
            # Pair only source objects from exactly the same input hierarchy.
            old_nodes = {n['global_node_id']: n for n in old_graph['nodes'] if n.get('source_detection_eligible') and n['detection_eligible']}
            new_nodes = {n['global_node_id']: n for n in new_graph['nodes'] if n.get('source_detection_eligible') and n['detection_eligible']}
            all_new = {n['global_node_id']: n for n in new_graph['nodes']}
            old_full = {n['global_node_id'] for c in old_result['crops'] if c['status'] == 'accepted' for n in c['targets']}
            new_full = {n['global_node_id'] for c in new_result['crops'] if c['status'] == 'accepted' for n in c['targets']}
            common = set(old_nodes) & set(new_nodes)
            paired['same_source_accepted_observations'] += 1
            paired['common_original_detection_targets'] += len(common)
            paired['common_original_targets_without_full_view_before'] += len(common-old_full)
            paired['common_original_targets_without_full_view_after'] += len(common-new_full)
            paired['common_original_targets_recovered'] += len((common-old_full) & new_full)
            paired['common_original_targets_newly_missing'] += len((common-new_full) & old_full)
            for key in set(old_nodes)-set(new_nodes):
                paired['original_targets_exiting_supervision'] += 1
                exits[all_new[key]['supervision_reason']] += 1
        summary['comparison']['paired_source_counts'] = dict(paired)
        summary['comparison']['source_exit_reasons'] = dict(exits)
        summary['comparison']['pairing_scope'] = 'Same input hierarchy hash, both accepted, originally detectable source objects only; generated IDs are not paired.'
    reports = root/'reports'
    write_json(reports/'slicing-summary.json', summary)
    write_json(reports/'slicing-problems.json', problem_rows)
    def csv_file(name, rows):
        if not rows:
            return
        with (reports/name).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    csv_file('slicing-observations.csv', observation_rows)
    csv_file('slicing-crops.csv', crop_rows)
    csv_file('slicing-cut-lines.csv', cut_rows)
    text = ['# 切片专项统计', '', '| 指标 | 数量 |', '|---|---:|']
    text.extend(f'| {k} | {v} |' for k, v in counts.items())
    text.extend(['', f'几何约束违规：`{dict(violations)}`', '', f'切线监督质量分级：`{dict(tiers)}`', '',
                 '| 分布 | 最小 | 中位 | P90 | P99 | 最大 |', '|---|---:|---:|---:|---:|---:|'])
    for key in ['long_crop_height', 'long_core_height', 'whole_image_height', 'adjacent_overlap_px', 'context_overhead_ratio', 'ignore_union_ratio', 'targets_per_crop']:
        d = summary['distributions'][key]
        text.append(f'| {key} | {d["min"]} | {d["p50"]} | {d["p90"]} | {d["p99"]} | {d["max"]} |')
    text.extend(['', '核心线穿过对象不等于目标丢失：实际裁图可以通过上下文保留完整目标。包装框丢失与内容目标丢失分别报告。', '',
                 f'长图实际裁片高度数量：`{summary["height_bins"]["long_actual_crops"]}`', '',
                 f'无需切分整图高度数量：`{summary["height_bins"]["whole_images"]}`', '',
                 f'相邻裁图重叠高度数量：`{summary["overlap_bins_px"]}`', '',
                 'ignore与正样本几何重叠需要下游损失实现正样本优先。低信息警告不是视觉错误真值。', '',
                 '详细记录见 slicing-problems.json、slicing-observations.csv、slicing-crops.csv、slicing-cut-lines.csv。'])
    (reports/'slicing-summary.md').write_text('\n'.join(text)+'\n')
    return summary
