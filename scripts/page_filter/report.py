"""Persist pre-/post-filter evidence summaries independently of label generation."""
from collections import Counter
import json
from pathlib import Path
from scripts.offline_postprocess.infrastructure import write_json, write_jsonl


def summarize_filtering(root):
    """Summarize viewport rejection and all sibling crop losses from immutable per-observation audits."""
    root = Path(root).resolve()
    run = json.loads((root/'run.json').read_text())
    pre_status, final_status, reasons, counts, kinds = Counter(), Counter(), Counter(), Counter(), Counter()
    failed_crops, rejected_views, layout_rejections = [], [], []
    for oid in run['observation_ids']:
        result = json.loads((root/'observations'/oid/'result.json').read_text())
        q = result['quality']
        pre = result.get('pre_filter', {'status':'review'})
        pre_status[pre['status']] += 1
        final_status[q['status']] += 1
        reasons.update(q['reason_codes'])
        pre_geometry = q.get('evidence',{}).get('pre_filter',{}).get('geometry',{})
        counts['source_layout_overflow_warning_observations'] += bool(pre_geometry.get('layout_warnings'))
        counts['source_layout_overflow_observations'] += bool(pre_geometry.get('layout_horizontal_outside'))
        gate = q.get('evidence',{}).get('pre_filter',{})
        if gate.get('status') == 'rejected':
            counts['source_geometry_gate_rejected_observations'] += 1
            layout_rejections.append({'observation_id':oid, 'page_id':q['page_id'], 'viewport':q['viewport'],
                                      'status':q['status'], 'reason_codes':gate['reason_codes'],
                                      'source_hash':q.get('source_hash'), 'geometry':pre_geometry})
        post = q.get('evidence',{}).get('post_filter',{})
        counts['post_filter_checked_observations'] += bool(post.get('checked'))
        if post.get('rejected'):
            counts['observations_rejected_due_to_blank_crop'] += 1
            counts['all_crop_views_withheld_for_blank_observations'] += len(result['crops'])
            counts['previously_usable_crop_views_withheld'] += sum(c.get('pre_quality_status')=='accepted' for c in result['crops'])
            rejected_views.append({'observation_id':oid, 'page_id':q['page_id'], 'viewport':q['viewport'],
                                   'reason_codes':q['reason_codes'], 'failed_crop_ids':post['failed_crop_ids'],
                                   'all_crop_views_withheld':len(result['crops']), 'source_hash':q.get('source_hash')})
        for check in post.get('crops',[]):
            counts['crop_views_checked'] += 1
            counts['actual_crops_flagged_blank'] += check['actual']['rejected']
            counts['cores_flagged_blank'] += bool(check['core'] and check['core']['rejected'])
            if check['rejected']:
                counts['distinct_crop_views_flagged_blank'] += 1
                kinds[check['actual']['kind'] if check['actual']['rejected'] else check['core']['kind']] += 1
                crop = next(c for c in result['crops'] if c['crop_id']==check['crop_id'])
                counts['flagged_crop_views_with_annotation_targets'] += bool(crop['targets'])
                failed_crops.append({'observation_id':oid, 'source_image':q['source_image'],
                                     'annotation_target_count':len(crop['targets']), **check})
        counts['solid_or_blank_whole_pages_rejected_pre_filter'] += 'blank_or_solid_page' in q['reason_codes']
    report={'schema':'gui_filter_summary_v3','rule_version':run['rule_version'],
            'config_fingerprint':run['config_fingerprint'],'observations':len(run['observation_ids']),
            'pre_filter_statuses':dict(pre_status),'final_statuses':dict(final_status),
            'reason_counts':dict(reasons),'counts':dict(counts),'blank_kinds':dict(kinds),
            'rejection_scope':'any blank actual/core crop rejects that entire viewport and all its detection/split supervision',
            'responsiveness':'unknown; geometry and layout warnings do not prove CSS responsiveness',
            'rejected_observations':rejected_views}
    report['pre_filter_policy'] = run['config']['pre_filter']
    write_json(root/'reports/filter-summary.json',report)
    write_jsonl(root/'reports/blank-crops.jsonl',failed_crops)
    write_jsonl(root/'reports/layout-rejections.jsonl',layout_rejections)
    lines=['# 两阶段filter统计','',f'预筛状态：`{dict(pre_status)}`；最终状态：`{dict(final_status)}`。','',
           '| 指标 | 数量 |','|---|---:|']
    lines.extend(f'| {key} | {value} |' for key,value in counts.items())
    lines.extend(['',f'原因：`{dict(reasons)}`','',
      '规则按原始RGB像素检查实际裁片与核心区域，不依赖框数量，也不读取画框后的图片。任一命中即剔除该视口的所有裁片与切线标签，其他正常视口独立保留。','',
      f"第一阶段几何预筛策略：{run['config']['pre_filter']['layout_overflow_policy']}；少框不自动判坏，CSS responsiveness仍为unknown。",'',
      '逐个命中区域、颜色范围、前景比例、边缘比例见blank-crops.jsonl。原始文件不删除。'])
    (root/'reports/filter-summary.md').write_text('\n'.join(lines)+'\n')
    return report
