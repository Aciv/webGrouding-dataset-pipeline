#!/usr/bin/env python3
"""Build a portable, read-only before/after layer gallery from an offline processing run."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil


def stats(tree):
    """Summarize visual granularity without equating automatic acceptance with annotation truth."""
    if tree is None:
        return None
    nodes = tree["nodes"]
    index = {n["node_id"]: n for n in nodes}
    roots = [n for n in nodes if n["parent_id"] is None]
    outside = []
    for n in nodes:
        parent = index.get(n["parent_id"])
        if not parent:
            continue
        x, y, w, h = n["bbox"]
        a, b, c, d = parent["bbox"]
        covered = max(0, min(x+w, a+c)-max(x, a))*max(0, min(y+h, b+d)-max(y, b))
        if covered/(w*h) < .95:
            outside.append(n["node_id"])
    return {"nodes": len(nodes), "layers": dict(sorted(Counter(n["layer"]+1 for n in nodes).items())),
        "roots": len(roots), "largest_root_height_ratio": round(max((n["bbox"][3] for n in roots), default=0)/tree["capture"]["height"], 3),
        "largest_root_area_ratio": round(max((n["bbox"][2]*n["bbox"][3] for n in roots), default=0)/(tree["capture"]["width"]*tree["capture"]["height"]), 4),
        "ignored": len(tree.get("ignore_regions", [])), "archived_l5": len(tree.get("detail_archive", {}).get("nodes", [])), "outside_parent_candidates": outside}


def build(output, baseline=None):
    """Copy/link screenshot assets and embed before/after trees; never mutates input observations."""
    report = json.loads((output/"layer-postprocess-report.json").read_text())
    records = []
    images = output/"comparison-images"
    images.mkdir(exist_ok=True)
    audits = {str(Path(json.loads(p.read_text())["source"]).resolve()): p for p in output.rglob("layer-review.json")}
    for pos, row in enumerate(sorted(report["observations"], key=lambda r: r["source"])):
        file = audits[str(Path(row["source"]).resolve())]
        audit = json.loads(file.read_text())
        source = Path(row["source"])
        before = json.loads(source.read_text())
        after_file = file.with_name("hierarchy.json")
        if not after_file.exists():
            after_file = file.with_name("candidate-hierarchy.json")
        after = json.loads(after_file.read_text()) if after_file.exists() else None
        previous = None
        if baseline:
            baseline_dir = baseline/file.parent.relative_to(output)
            previous_file = next((baseline_dir/name for name in ("hierarchy.json", "candidate-hierarchy.json", "rule-hierarchy.json") if (baseline_dir/name).exists()), None)
            if previous_file:
                previous = json.loads(previous_file.read_text())
        if baseline is None and file.with_name("rule-hierarchy.json").exists():
            previous = json.loads(file.with_name("rule-hierarchy.json").read_text())
        image = source.parent/before["capture"]["image"]
        destination = images/f"{pos:03}.png"
        if not destination.exists():
            try:
                os.link(image, destination)
            except OSError:
                shutil.copyfile(image, destination)
        def slim(tree):
            if tree is None:
                return None
            main = [{k: n.get(k) for k in ("node_id", "parent_id", "layer", "bbox", "struct")} for n in tree["nodes"]]
            details = [{"node_id": n["node_id"], "parent_id": None, "owner_id": n.get("owner_id"), "layer": 4,
                "bbox": n["bbox"], "struct": n.get("struct"), "archive": True, "archive_reason": n.get("archive_reason")} for n in tree.get("detail_archive", {}).get("nodes", [])]
            return {"capture": tree["capture"], "nodes": main+details}
        records.append({"name": str(file.parent.relative_to(output)), "status": row["status"],
            "seconds": row["seconds"], "image": destination.relative_to(output).as_posix(),
            "before": slim(before), "previous": slim(previous), "after": slim(after),
            "stats": {"before": stats(before), "previous": stats(previous), "after": stats(after)},
            "issues": audit.get("verification", {}).get("issues", [])+audit.get("large_region_validation_errors", [])+audit.get("review_blockers", []), "error": audit.get("error"),
            "rejected_operations": audit.get("rejected_operations", []),
            "large_region_checks": audit.get("large_region_checks", []),
            "pixel_candidates": audit.get("pixel_candidates", []), "pixel_evidence": audit.get("pixel_evidence", []),
            "fusion_input": audit.get("fusion_input", {}), "pre_archive_fusion": audit.get("pre_archive_fusion", {}),
            "applied_upper_replacements": audit.get("applied_upper_replacements", []),
            "top_down_cleanup": audit.get("top_down_cleanup", {}), "layer_accumulation": audit.get("layer_accumulation", {}),
            "layout": audit.get("rule_summary", {}).get("layout_regions", [])})
    data = json.dumps(records, ensure_ascii=False).replace("<", "\\u003c")
    html = TEMPLATE.replace("__DATA__", data)
    if baseline is not None:
        html = html.replace('value="previous"', 'value="previous" selected', 1)
    (output/"comparison.html").write_text(html)
    summary = {"counts": report["counts"], "wall_seconds": report["seconds"], "samples": [
        {k: r[k] for k in ("name", "status", "seconds", "stats", "issues", "error", "layout", "rejected_operations", "large_region_checks")} for r in records]}
    (output/"comparison-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({"gallery": str(output/"comparison.html"), "counts": report["counts"], "samples": len(records)}, ensure_ascii=False))


TEMPLATE = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>层级后处理对比</title>
<style>*{box-sizing:border-box}body{margin:0;font:14px system-ui;background:#f0f3f6;color:#1c2937}header{position:sticky;top:0;background:#fff;z-index:10;padding:12px 20px;box-shadow:0 2px 8px #0002}select,button{padding:6px;margin:3px}#levels label{display:inline-block;margin:5px}#info{margin:6px;color:#45576b}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:12px;align-items:start}.panel{background:white;min-width:0}.title{padding:10px;border-bottom:1px solid #ddd}figure{position:relative;margin:0}img{display:block;width:100%;height:auto}svg{position:absolute;inset:0;width:100%;height:100%}rect{fill:none;stroke-width:2;vector-effect:non-scaling-stroke}text{font:12px monospace;fill:#006b25;paint-order:stroke;stroke:white;stroke-width:3px}details{padding:8px}pre{white-space:pre-wrap;max-height:230px;overflow:auto}#hint{font-size:12px;color:#637083}</style>
<header><strong>层级粒度：前后对比</strong> <button id="prev">上一页</button><select id="page"></select><button id="next">下一页</button>
<select id="mode"><option value="before">对比原始标注</option><option value="previous">对比规则基线/旧后处理（有则显示）</option></select>
<div id="levels"></div><label><input id="ids" type="checkbox">显示节点编号</label><label><input id="pixels" type="checkbox">显示像素候选：虚线分区／实线目标（不代表获批）</label><button id="all">全选</button><button id="first">只看第一级</button><button id="coarse">只看 L1–4</button><button id="deep">只看 L5+</button>
<div id="info"></div><div id="hint">级别 = layer+1；可自由多选。绿色框可悬停查看 ID、parent 或归档 owner。L5 归档默认不训练；只看 L1–4 可隐藏归档。accepted 是局部结构处理状态，不等于人工标注正确。</div></header>
<main class="grid"><section class="panel"><div class="title" id="leftTitle"></div><figure id="left"></figure></section><section class="panel"><div class="title" id="rightTitle"></div><figure id="right"></figure></section></main>
<details><summary>查看审核问题和分组记录</summary><pre id="detail"></pre></details>
<script>const records=__DATA__;const $=id=>document.getElementById(id);const NS='http://www.w3.org/2000/svg';let enabled=new Set([0]);
records.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.name+' · '+r.status;$('page').append(o)});
const maxLayer=Math.max(3,...records.flatMap(r=>[r.before,r.after,r.previous].filter(Boolean).flatMap(t=>t.nodes.map(n=>n.layer))));
for(let i=0;i<=maxLayer;i++){const l=document.createElement('label'),c=document.createElement('input');c.type='checkbox';c.checked=i===0;c.dataset.layer=i;c.onchange=()=>{c.checked?enabled.add(i):enabled.delete(i);show()};l.append(c,document.createTextNode('第'+(i+1)+'级'+(i===4?'（细节/归档）':'')));$('levels').append(l)}
function render(id,tree,r){const figure=$(id);figure.replaceChildren();if(!tree){figure.textContent='没有可用结果，请查看报告';return}const img=document.createElement('img');img.src=r.image;figure.append(img);const svg=document.createElementNS(NS,'svg');svg.setAttribute('viewBox',`0 0 ${tree.capture.width} ${tree.capture.height}`);figure.append(svg);for(const n of tree.nodes){if(!enabled.has(n.layer))continue;const [x,y,w,h]=n.bbox,rect=document.createElementNS(NS,'rect');for(const [k,v]of Object.entries({x,y,width:w,height:h,stroke:['#008a37','#008b76','#58a100','#0b6c34'][n.layer%4]}))rect.setAttribute(k,v);const title=document.createElementNS(NS,'title');title.textContent=`${n.node_id} · ${n.struct} · ${n.archive?'归档 owner='+n.owner_id+' · '+n.archive_reason:'parent='+n.parent_id} · 第${n.layer+1}级`;rect.append(title);svg.append(rect);if($('ids').checked){const t=document.createElementNS(NS,'text');t.setAttribute('x',x+2);t.setAttribute('y',y+13);t.textContent=n.node_id;svg.append(t)}}if(id==='right'&&$('pixels').checked){for(const p of r.pixel_candidates||[]){p.parts.forEach((part,i)=>{for(const [field,dash] of [['partition_bbox','7 5'],['target_bbox','']]){const [x,y,w,h]=part[field],q=document.createElementNS(NS,'rect');for(const [k,v]of Object.entries({x,y,width:w,height:h,stroke:'#00a83c','stroke-dasharray':dash}))q.setAttribute(k,v);const tip=document.createElementNS(NS,'title');tip.textContent=p.candidate_id+' / '+i+' / '+field+' / '+part.target_basis;q.append(tip);svg.append(q)}})}}}
function show(){const r=records[Number($('page').value)];if(!r)return;const previous=$('mode').value==='previous'&&r.previous;const left=previous||r.before;render('left',left,r);render('right',r.after,r);$('leftTitle').textContent=(previous?'规则基线/旧版后处理':'原始标注')+' · '+left.nodes.length+'框';$('rightTitle').textContent='新版'+(r.status==='accepted'?'结构通过':'候选')+' · '+(r.stats.after?r.stats.after.nodes:0)+'主体框 + '+(r.stats.after?r.stats.after.archived_l5||0:0)+'归档框';$('info').textContent=r.name+' · '+r.seconds+'s · '+JSON.stringify(r.stats);$('detail').textContent=JSON.stringify({issues:r.issues,error:r.error,layout:r.layout,rejected_operations:r.rejected_operations,pixel_candidates:r.pixel_candidates,pixel_evidence:r.pixel_evidence,top_down_cleanup:r.top_down_cleanup,accumulation:r.layer_accumulation,fusion_input:r.fusion_input,pre_archive_fusion:r.pre_archive_fusion,applied_upper_replacements:r.applied_upper_replacements},null,2);$('prev').disabled=$('page').value==0;$('next').disabled=Number($('page').value)===records.length-1;}
$('page').onchange=show;$('mode').onchange=show;$('ids').onchange=show;$('pixels').onchange=show;$('prev').onclick=()=>{$('page').value=Number($('page').value)-1;show()};$('next').onclick=()=>{$('page').value=Number($('page').value)+1;show()};function check(all){enabled=new Set(all?Array.from({length:maxLayer+1},(_,i)=>i):[0]);document.querySelectorAll('[data-layer]').forEach(c=>c.checked=enabled.has(Number(c.dataset.layer)));show()}$('all').onclick=()=>check(true);$('first').onclick=()=>check(false);function selectDepths(xs){enabled=new Set(xs);document.querySelectorAll('[data-layer]').forEach(c=>c.checked=enabled.has(Number(c.dataset.layer)));show()}$('coarse').onclick=()=>selectDepths([0,1,2,3]);$('deep').onclick=()=>selectDepths(Array.from({length:Math.max(0,maxLayer-3)},(_,i)=>i+4));show();</script></html>'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    build(args.output.resolve(), args.baseline.resolve() if args.baseline else None)
