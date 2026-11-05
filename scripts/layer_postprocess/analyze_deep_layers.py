#!/usr/bin/env python3
"""Compare old deep previews and strict cleanup offline; never edits training/source labels."""
import argparse
from collections import Counter,defaultdict
import json
from pathlib import Path
from rules import validate
from storage import write


def chain_type(chain):
    """Classify saved structural paths as diagnostics, not verified semantic ground truth."""
    structs={n.get('struct') for n in chain};subtypes={n.get('subtype') for n in chain}
    if 'table' in structs:return 'table_chain'
    if 'list' in structs:return 'list_menu_chain'
    if 'unverified_control_hint' in subtypes:return 'uncertain_control_chain'
    return 'content_region_chain'


def analyze(baseline, output):
    """Report every old L5+ target and its new fate, preserving the original diagnostic scope."""
    samples=[];old_types=Counter();old_levels=Counter();patterns=Counter();pattern_pages=defaultdict(set)
    old_total=0;new_total=0;fates=Counter();strict_reasons=Counter();unchanged_geometry=True
    for old_audit in sorted(baseline.rglob('layer-review.json')):
        old=json.loads(old_audit.read_text())
        if old.get('rule_summary',{}).get('after_levels',0)<=4:continue
        rel=old_audit.parent.relative_to(baseline);new_audit=output/rel/'layer-review.json'
        if not new_audit.exists():raise ValueError('Missing strict result: '+str(rel))
        new=json.loads(new_audit.read_text());before=json.loads(old_audit.with_name('rule-hierarchy.json').read_text());after=json.loads(new_audit.with_name('rule-hierarchy.json').read_text())
        bi={n['node_id']:n for n in before['nodes']};ai={n['node_id']:n for n in after['nodes']};archive={n['node_id']:n for n in after.get('detail_archive',{}).get('nodes',[])}
        bd,ad=validate(before),validate(after);old_deep=[n for n in before['nodes'] if bd[n['node_id']]>=4];new_deep=[n for n in after['nodes'] if ad[n['node_id']]>=4]
        strict_ops=[op for op in new.get('rule_operations',[]) if op['reason'].startswith('rule_strict_')]
        strict_ids={op['node']:op for op in strict_ops};strict_reasons.update(op['reason'] for op in strict_ops)
        nodes=[];local_patterns=Counter();local_fates=Counter()
        for n in old_deep:
            key=n['node_id'];old_types[n['struct']]+=1;old_levels[str(bd[key]+1)]+=1
            ancestors=[];current=n
            while current:
                ancestors.append(current);current=bi.get(current['parent_id'])
            category=chain_type(ancestors);patterns[category]+=1;local_patterns[category]+=1;pattern_pages[category].add(str(rel))
            if key in ai:
                fate='retained_deep' if ad[key]>=4 else 'promoted_to_l1_l4'
                if ai[key]['bbox']!=n['bbox']:unchanged_geometry=False
            elif key in archive:fate='retained_in_l5_archive'
            elif key in strict_ids:fate='omitted_by_strict_noise_policy'
            else:fate='contracted_or_other_rule_cleanup'
            fates[fate]+=1;local_fates[fate]+=1
            nodes.append({'node_id':key,'old_level':bd[key]+1,'new_level':ad.get(key,-1)+1 if key in ai else 5 if key in archive else None,
                'bbox':n['bbox'],'struct':n['struct'],'subtype':n.get('subtype'),'fate':fate,'pattern':category,
                'path':[{'id':v['node_id'],'level':bd[v['node_id']]+1,'struct':v['struct'],'subtype':v.get('subtype'),'tag':v.get('tag')} for v in reversed(ancestors)]})
        old_total+=len(old_deep);new_total+=len(new_deep)
        samples.append({'sample':str(rel),'old_nodes':len(bi),'new_nodes':len(ai),'old_max_level':max(bd.values())+1,'new_max_level':max(ad.values())+1,
            'old_deep':len(old_deep),'new_deep':len(new_deep),'strict_noise_omissions':len(strict_ops),'patterns':dict(local_patterns),'old_deep_fates':dict(local_fates),'nodes':nodes})
    report={'scope':'The same 42 original deep previews, before any model calls; no API calls in this analysis.',
        'samples':len(samples),'old_deep_nodes':old_total,'new_deep_nodes':new_total,
        'old_deep_levels':dict(old_levels),'old_deep_struct':dict(old_types),'old_deep_fates':dict(fates),
        'strict_omission_reasons':dict(strict_reasons),'samples_now_at_most_four':sum(s['new_max_level']<=4 for s in samples),
        'samples_still_deep':sum(s['new_max_level']>4 for s in samples),
        'path_patterns':dict(patterns),'pattern_sample_counts':{k:len(v) for k,v in pattern_pages.items()},
        'retained_old_node_geometry_unchanged':unchanged_geometry,'sample_details':samples,
        'caveat':'Structural tags and thresholds are diagnostic heuristics; retained does not mean important, omitted does not mean invisible. No human ground-truth accuracy assessment.'}
    write(output/'deep-layer-analysis.json',report)
    lines=['# 原 42 份深层预览：模式诊断与严格清理对比','',
        '- 未调用模型；没有覆盖 datasets、07 或 08 预览。统计对象始终是同一批 42 份。',
        '- 先前 271 框是“模型加层”的另一项诊断；这里的 1228 框是保留原始主要内容后的规则预览，不能混为一组。',
        f'- 旧 L5+ 共 {old_total} 框；严格规则后 L5+ 共 {new_total} 框。',
        f"- {report['samples_now_at_most_four']} 份达到最多四层；{report['samples_still_deep']} 份仍有较深内容。",'',
        '## 反复出现的层级模式','', '| 路径模式 | 旧深层框数 | 涉及样本 |','|---|---:|---:|']
    labels={'list_menu_chain':'列表/菜单/卡片列表链','table_chain':'表格内部链','uncertain_control_chain':'不确定控件包装链','content_region_chain':'内容区域/卡片/表单链'}
    for key,count in patterns.most_common():lines.append(f'| {labels[key]} | {count} | {len(pattern_pages[key])} |')
    lines+=['','单个样本可包含多类路径；表格按优先级把每个深层框分到一类，样本数不能相加。',
        '例如：section→ul→卡片→figure→图片；section→tabpanel→卡片→布局→form→输入；table→unknown 包装→unknown 包装→链接。',
        '这些路径深度不等于视觉语义深度；命名为 text/control 也不证明它值得作为独立训练目标。','',
        '## 原有深层框的去向','']
    for key,count in fates.items():lines.append(f'- {key}：{count}。')
    lines+=['','## 逐样本','', '| 样本 | 旧最大层 | 新最大层 | 旧 L5+ | 新 L5+ | 严格剔除（原树范围） |','|---|---:|---:|---:|---:|---:|']
    for s in sorted(samples,key=lambda x:(-x['new_deep'],-x['old_deep'])):
        lines.append(f"| {s['sample']} | {s['old_max_level']} | {s['new_max_level']} | {s['old_deep']} | {s['new_deep']} | {s['strict_noise_omissions']} |")
    lines+=['','## 查看方式','',
        '- comparison.html 默认对比旧规则预览与新严格规则预览。点击“只看 L5+”可以直接定位剩余深层框。',
        '- JSON 保存原来每个深层框的原层级、当前层级、父链和处理去向，可批量分类，不要求逐页人工确认。',
        '- 下一步应按列表、figure、表格/表单和 unknown 包装分别规范，而不是继续给这些路径增加分组层。',
        '- 聚合下层块的候选已改为拆分/替换上层；新增区域与旧上层同级，原有成员不加深。尚未对本批预览重新付费调用模型。']
    (output/'DEEP_LAYER_ANALYSIS.md').write_text('\n'.join(lines)+'\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--baseline',required=True,type=Path);parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    result=analyze(args.baseline.resolve(),args.output.resolve())
    print(json.dumps({k:v for k,v in result.items() if k!='sample_details'},ensure_ascii=False))
