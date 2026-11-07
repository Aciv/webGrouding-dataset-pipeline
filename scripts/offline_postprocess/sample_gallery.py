"""Reproducible random full-page versus actual-crop visualization, separate from training data."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED
import hashlib
import html
import json
import math
import random
import re

from PIL import Image, ImageDraw, ImageFont
from .domain import xyxy
from .infrastructure import read_jsonl, sha256_file, write_json

_GREEN = '#079447'
_ORANGE = '#dc8700'
_BLUE = '#2164ed'
_MAGENTA = '#d125b4'


def _font(size=17):
    """Select a local report font without requiring downloads or altering captured pixels."""
    path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default()


def _rect(draw, box, color, size, width=2):
    """Draw a CSS xyxy rectangle clipped to this exported image's native pixel extent."""
    w, h = size
    left, top = max(0, box[0]), max(0, box[1])
    right, bottom = min(w-1, box[2]), min(h-1, box[3])
    if right >= left and bottom >= top:
        draw.rectangle([left, top, right, bottom], outline=color, width=width)


def _core_lines(draw, core, crop, size):
    """Draw core boundaries in crop-local y, keeping the exported raster at its actual crop size."""
    w, h = size
    for global_y in (core[1], core[3]):
        y = global_y-crop[1]
        if 0 < y < h:
            for x in range(0, w, 20):
                draw.line([x, y, min(w-1, x+12), y], fill=_BLUE, width=3)


def _save(image, path):
    """Write a review-only JPEG without rescaling; source screenshots remain read-only."""
    image.save(path, 'JPEG', quality=93, subsampling=0, optimize=True)


def _comparison(directory, record):
    """Render an aspect-preserving overview of the whole page and every crop, with native-image links elsewhere."""
    margin, gap, left_width, crop_width = 20, 18, 480, 400
    title_height = 106
    crops = record['crops']
    columns = min(3, max(1, len(crops)))
    row_heights = []
    for start in range(0, len(crops), columns):
        sizes = [crop['image_size'] for crop in crops[start:start+columns]]
        row_heights.append(62+max(round(h*min(crop_width/w, 640/h, 1)) for w, h in sizes))
    right_height = sum(row_heights)
    full_height = min(2000, record['original_size']['height'])
    full_scale = min(left_width/record['original_size']['width'], full_height/record['original_size']['height'], 1)
    rendered_full_height = round(record['original_size']['height']*full_scale)
    height = title_height+max(right_height, rendered_full_height+52)+margin
    width = margin+left_width+gap+columns*(crop_width+gap)+margin
    font, small = _font(19), _font(15)
    with Image.new('RGB', (width, height), '#f2f4f7') as sheet:
        draw = ImageDraw.Draw(sheet)
        draw.text((margin, 12), f"{record['number']:02d}  {record['observation_id']}", fill='#152536', font=font)
        draw.text((margin, 42), f"whole={record['original_size']['width']}x{record['original_size']['height']}  graph targets={record['final_target_count']}  crops={len(crops)}  status={record['status']}", fill='#152536', font=small)
        draw.text((margin, 66), 'green=complete target | orange=generated region | magenta=ignore | blue=core boundary', fill='#152536', font=small)
        draw.text((margin, title_height), 'Original full page + final boxes', fill='#152536', font=small)
        with Image.open(directory/'final-boxes.jpg') as original:
            original.thumbnail((left_width, full_height))
            sheet.paste(original, (margin+(left_width-original.width)//2, title_height+36))
        for position, crop in enumerate(crops):
            x = margin+left_width+gap+(position % columns)*(crop_width+gap)
            y = title_height+sum(row_heights[:position//columns])
            draw.text((x, y), f"crop {crop['index']:02d}  {crop['status']}  +{crop['target_count']} / ignore {crop['ignore_count']}", fill='#152536', font=small)
            draw.text((x, y+22), f"global y={crop['crop_bbox'][1]}..{crop['crop_bbox'][3]}  core={crop['core_bbox'][1]}..{crop['core_bbox'][3]}", fill='#152536', font=small)
            with Image.open(directory/crop['audit_image']) as image:
                image.thumbnail((crop_width, 640))
                sheet.paste(image, (x+(crop_width-image.width)//2, y+50))
        _save(sheet, directory/'comparison.jpg')


def _render_one(root, output, item):
    """Decode one source once, then export native xyxy overlays for the full page and all actual crops."""
    number, observation_id = item
    source_directory = root/'observations'/observation_id
    result = json.loads((source_directory/'result.json').read_text())
    graph = json.loads((source_directory/'hierarchy.json').read_text())
    quality = result['quality']
    source_file = Path(quality['source_hierarchy'])
    if (sha256_file(source_file) != quality['source_hash']
            or sha256_file(quality['source_image']) != quality['image_sha256']
            or sha256_file(source_directory/'hierarchy.json') != result['artifact_sha256']):
        raise ValueError(f'Source/derived fingerprint mismatch: {observation_id}')
    source = json.loads(source_file.read_text())
    directory = output/f'{number:02d}'
    directory.mkdir()
    final_targets = [n for n in graph['nodes'] if n['detection_eligible']]
    generated = {n['global_node_id'] for n in final_targets if n.get('generated')}
    full_ids = {n['global_node_id'] for c in result['crops'] if c['status'] == 'accepted' for n in c['targets']}
    record = {'number': number, 'observation_id': observation_id, 'page_id': quality['page_id'],
              'viewport': quality['viewport'], 'status': quality['status'],
              'reason_codes': quality['reason_codes'],
              'blank_crop_ids': quality.get('evidence',{}).get('post_filter',{}).get('failed_crop_ids',[]),
              'directory': directory.name, 'original_size': quality['original_size'],
              'source_image': quality['source_image'], 'source_hierarchy': str(source_file),
              'source_hierarchy_sha256': quality['source_hash'], 'source_image_sha256': quality['image_sha256'],
              'derived_hierarchy_sha256': sha256_file(source_directory/'hierarchy.json'),
              'source_detection_count': sum(n.get('disposition') == 'detection' for n in source['nodes']),
              'final_target_count': len(final_targets), 'published_unique_target_count': len(full_ids),
              'cut_lines': result['split_label']['cut_lines_original_px'] if result.get('split_label') else [],
              'split_supervision_eligible': bool(result.get('split_label') and result['split_label']['supervision_eligible']),
              'missing_target_ids': quality.get('evidence', {}).get('unrepresented_target_ids', []),
              'crops': []}
    with Image.open(quality['source_image']) as opened:
        with opened.convert('RGB') as original:
            _save(original, directory/'original.jpg')
            with original.copy() as annotated:
                draw = ImageDraw.Draw(annotated)
                for node in source['nodes']:
                    if node.get('disposition') == 'detection':
                        _rect(draw, xyxy(node['bbox']), _GREEN, annotated.size)
                _save(annotated, directory/'source-boxes.jpg')
            with original.copy() as annotated:
                draw = ImageDraw.Draw(annotated)
                for node in sorted(final_targets, key=lambda n: -(n['bbox'][2]-n['bbox'][0])*(n['bbox'][3]-n['bbox'][1])):
                    _rect(draw, node['bbox'], '#909090' if quality['status'] != 'accepted' else _ORANGE if node.get('generated') else _GREEN, annotated.size)
                for y in record['cut_lines']:
                    draw.line([0, y, original.width-1, y], fill=_BLUE, width=3)
                _save(annotated, directory/'final-boxes.jpg')
            global_index = {n['global_node_id']: n for n in final_targets}
            for pos, crop in enumerate(result['crops']):
                box_name, audit_name = f'crop-{pos:02d}-boxes.jpg', f'crop-{pos:02d}-audit.jpg'
                b = crop['crop_bbox']
                with original.crop(b) as image:
                    draw = ImageDraw.Draw(image)
                    _core_lines(draw, crop['core_bbox'], b, image.size)
                    for target in crop['targets']:
                        node = global_index[target['global_node_id']]
                        expected = [node['bbox'][0], node['bbox'][1]-b[1], node['bbox'][2], node['bbox'][3]-b[1]]
                        if any(abs(a-c) > 1e-7 for a, c in zip(expected, target['bbox'])):
                            raise ValueError(f'Global/local target mismatch: {observation_id}')
                        _rect(draw, target['bbox'], '#909090' if quality['status'] != 'accepted' else _ORANGE if target['global_node_id'] in generated else _GREEN, image.size)
                    _save(image, directory/box_name)
                with original.crop(b) as image:
                    draw = ImageDraw.Draw(image)
                    for ignore in crop['ignore_regions']:
                        _rect(draw, ignore['bbox'], _MAGENTA, image.size)
                    _core_lines(draw, crop['core_bbox'], b, image.size)
                    for target in crop['targets']:
                        _rect(draw, target['bbox'], '#909090' if quality['status'] != 'accepted' else _ORANGE if target['global_node_id'] in generated else _GREEN, image.size)
                    _save(image, directory/audit_name)
                record['crops'].append({'index': pos, 'crop_id': crop['crop_id'],
                    'status': crop['status'], 'reason_codes': crop['reason_codes'],
                    'core_bbox': crop['core_bbox'], 'crop_bbox': b,
                    'image_size': [b[2]-b[0], b[3]-b[1]], 'bbox_format': 'xyxy',
                    'target_count': len(crop['targets']), 'ignore_count': len(crop['ignore_regions']),
                    'target_ids': [n['global_node_id'] for n in crop['targets']],
                    'boxes_image': box_name, 'audit_image': audit_name})
    write_json(directory/'metadata.json', record)
    _comparison(directory, record)
    return record


def _overview(output, records):
    """Create one compact 32-case contact sheet; per-case exports preserve native detail."""
    columns, cell_w, cell_h = 4, 440, 350
    font = _font(14)
    with Image.new('RGB', (columns*cell_w, math.ceil(len(records)/columns)*cell_h), '#edf0f4') as sheet:
        draw = ImageDraw.Draw(sheet)
        for pos, record in enumerate(records):
            x, y = (pos % columns)*cell_w, (pos//columns)*cell_h
            draw.text((x+7, y+6), f"{record['number']:02d} {record['observation_id']}", fill='#152536', font=font)
            draw.text((x+7, y+27), f"{len(record['crops'])} crops | {record['final_target_count']} final targets", fill='#152536', font=font)
            with Image.open(output/record['directory']/'comparison.jpg') as image:
                image.thumbnail((cell_w-14, cell_h-60))
                sheet.paste(image, (x+(cell_w-image.width)//2, y+53))
        _save(sheet, output/'overview.jpg')


def _write_html(output, manifest):
    """Write an offline relative-path gallery with original/source/final toggles and every crop in order."""
    cards = []
    for record in manifest['samples']:
        directory = html.escape(record['directory'], quote=True)
        oid = html.escape(record['observation_id'])
        crops = []
        for crop in record['crops']:
            src = f"{directory}/{html.escape(crop['boxes_image'], quote=True)}"
            audit = f"{directory}/{html.escape(crop['audit_image'], quote=True)}"
            crops.append(f'''<figure class="crop-card"><figcaption>裁片 {crop['index']:02d} · {crop['status']} · 框 {crop['target_count']} · ignore {crop['ignore_count']}<br>原图 y={crop['crop_bbox'][1]}–{crop['crop_bbox'][3]}，核心 y={crop['core_bbox'][1]}–{crop['core_bbox'][3]}</figcaption><a class="crop-link" href="{src}" target="_blank"><img loading="lazy" class="crop-image" src="{src}" data-boxes="{src}" data-audit="{audit}" alt="{oid} crop {crop['index']}"></a></figure>''')
        cards.append(f'''<article id="sample-{record['number']:02d}" data-search="{oid.lower()}"><h2>{record['number']:02d} · {oid}</h2><p class="meta">原图 {record['original_size']['width']}×{record['original_size']['height']} · 源候选 {record['source_detection_count']} → 最终目标 {record['final_target_count']} · {len(record['crops'])} 个裁图视图 · 切线监督 {str(record['split_supervision_eligible']).lower()} · 无完整视图目标 {len(record['missing_target_ids'])}</p><div class="comparison"><figure class="whole"><figcaption>完整原图加框；蓝线为核心切线。点击图片查看原分辨率。<br><a href="{directory}/comparison.jpg" target="_blank">打开整组对照图</a> · <a href="{directory}/metadata.json" target="_blank">坐标与目标清单</a></figcaption><a class="whole-link" href="{directory}/final-boxes.jpg" target="_blank"><img loading="lazy" class="whole-image" src="{directory}/final-boxes.jpg" data-final="{directory}/final-boxes.jpg" data-source="{directory}/source-boxes.jpg" data-original="{directory}/original.jpg" alt="{oid} full page"></a></figure><div class="crop-grid">{''.join(crops)}</div></div></article>''')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>32个随机样本：整图与最终裁片</title><style>
*{box-sizing:border-box}body{margin:0;font:15px/1.6 system-ui,sans-serif;background:#f1f4f8;color:#17273a}header,main{max-width:1800px;margin:auto;padding:20px}h1{font-size:25px;margin:0 0 8px}h2{font-size:19px;margin:0}p{margin:8px 0}.controls{position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid #ccd4df;padding:12px 20px;display:flex;gap:18px;flex-wrap:wrap;align-items:center}select,input{font:inherit;padding:5px}article{margin:18px 0;padding:18px;background:white;border:1px solid #d7dfe8;border-radius:8px}a{color:#1b57b5}.meta{color:#53677d}.comparison{display:grid;grid-template-columns:minmax(220px,32%) 1fr;gap:16px;align-items:start}figure{margin:0;padding:8px;border:1px solid #dce2e9;background:#fafbfd}figcaption{font-size:13px;color:#41536a;margin-bottom:8px;overflow-wrap:anywhere}.whole-image{width:100%;height:auto;max-height:1800px;object-fit:contain;object-position:top}.crop-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}.crop-image{width:100%;height:auto;max-height:680px;object-fit:contain;object-position:top}.expanded img{max-height:none}.hidden{display:none}.legend{font-size:14px}.green{color:#079447}.orange{color:#b56b00}.blue{color:#2164ed}.magenta{color:#c0179f}@media(max-width:800px){.comparison{grid-template-columns:1fr}.crop-grid{grid-template-columns:1fr}.whole-image{max-height:900px}}
</style><header><h1>随机32个视口：整图框与最终裁片框</h1><p>从v2最终可用视口均匀无放回抽样，固定随机种子 <b>__SEED__</b>，候选池 __POPULATION__。包含未触发切分的整图；保留全部裁图视图，未发布的裁片明确标为 review。</p><p class="legend"><span class="green">绿色：完整检测目标</span> · <span class="orange">橙色：规则新增区域</span> · <span class="blue">蓝色：核心切线</span> · <span class="magenta">紫色：ignore（可切换）</span>。标签为规则候选；有框不等于人工确认正确。</p><p><a href="overview.jpg" target="_blank">32例总览</a> · <a href="manifest.json" target="_blank">抽样与文件清单</a> · <a href="verification.json" target="_blank">导出完整性检查</a>。JPG仅供复核，不能作为训练输入。</p></header><div class="controls"><label>整图模式 <select id="mode"><option value="final">最终检测框</option><option value="source">原始候选框</option><option value="original">无框原图</option></select></label><label><input id="ignores" type="checkbox">显示裁片ignore</label><label><input id="expand" type="checkbox">展开图片高度</label><label>筛选样本 <input id="search" type="search" placeholder="页面ID / mobile / tablet"></label><span id="visible-count"></span></div><main>__CARDS__</main><script>
const articles=[...document.querySelectorAll('article')];
document.getElementById('mode').onchange=e=>document.querySelectorAll('.whole-image').forEach(img=>{img.src=img.dataset[e.target.value];img.closest('a').href=img.src});
document.getElementById('ignores').onchange=e=>document.querySelectorAll('.crop-image').forEach(img=>{img.src=e.target.checked?img.dataset.audit:img.dataset.boxes;img.closest('a').href=img.src});
document.getElementById('expand').onchange=e=>document.body.classList.toggle('expanded',e.target.checked);
function filter(){const q=document.getElementById('search').value.toLowerCase().trim();let n=0;articles.forEach(a=>{const show=a.dataset.search.includes(q);a.classList.toggle('hidden',!show);if(show)n++});document.getElementById('visible-count').textContent=n+' / '+articles.length+' 个样本'}document.getElementById('search').oninput=filter;filter();
</script></html>'''
    if manifest.get('original_selection_manifest'):
        description = f"<p>沿用上一版的{manifest['rendered_count']}个随机视口和编号，不替换被剔除样本。原种子 {manifest['seed']}；本版状态：{html.escape(str(manifest['summary']['observation_statuses']))}。灰色框仅供审计，rejected视口的所有裁片均不进入训练。</p>"
        page = re.sub(r'<p>从v2最终可用视口.*?</p>', description, page, count=1)
    else:
        page = page.replace('从v2最终可用视口', f"从{manifest['rule_version']}最终可用视口")
    page = page.replace('__SEED__', str(manifest['seed'])).replace('__POPULATION__', str(manifest['population_size'])).replace('__CARDS__', ''.join(cards))
    for record in manifest['samples']:
        if record['status'] != 'accepted':
            heading = f"<h2>{record['number']:02d} · {html.escape(record['observation_id'])}</h2>"
            reason = html.escape(' / '.join(record['reason_codes']))
            page = page.replace(heading, heading+f'<p style="color:#b91c1c;font-weight:bold">已剔除／待审：{reason}。以下为审计图，全部不进入训练。</p>')
    page = page.replace('32个随机样本', f"{manifest['rendered_count']}个随机样本").replace('随机32个视口', f"随机{manifest['rendered_count']}个视口").replace('32例总览', f"{manifest['rendered_count']}例总览")
    (output/'index.html').write_text(page, encoding='utf-8')


def generate_gallery(root, output, count=32, seed=20261107, workers=4, archive=True, selection_manifest=None):
    """Sample published viewports reproducibly and export native overlay assets plus a portable offline gallery."""
    root, output = Path(root).resolve(), Path(output).resolve()
    if type(count) is not int or count < 1 or not 1 <= workers <= 16:
        raise ValueError('Positive sample count and workers in 1..16 are required')
    if output == root or output in root.parents:
        raise ValueError('Gallery output must not replace or contain the dataset root')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Gallery output must be empty; choose a new directory')
    published = {r['observation_id'] for r in read_jsonl(root/'crops/trainable.jsonl')}
    population = sorted(q['observation_id'] for q in read_jsonl(root/'quality/index.jsonl')
                        if q['status'] == 'accepted' and q['observation_id'] in published)
    if count > len(population) and not selection_manifest:
        raise ValueError('Sample count exceeds eligible viewport population')
    if selection_manifest:
        prior = json.loads(Path(selection_manifest).read_text())
        chosen = [r['observation_id'] for r in prior['samples']]
        if len(chosen) != count or len(set(chosen)) != count:
            raise ValueError('Frozen selection must match requested count and contain unique viewport IDs')
        seed = prior['seed']
    else:
        chosen = random.Random(seed).sample(population, count)
    output.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_render_one, root, output, (number, oid)) for number, oid in enumerate(chosen, 1)]
        records, failures = [], []
        for number, future in enumerate(futures, 1):
            try:
                record = future.result()
                records.append(record)
                print(json.dumps({'rendered': len(records), 'total': count, 'observation_id': record['observation_id']}, ensure_ascii=False), flush=True)
            except Exception as error:
                failures.append({'number': number, 'observation_id': chosen[number-1], 'error': f'{type(error).__name__}: {error}'})
    run = json.loads((root/'run.json').read_text())
    manifest = {'schema': 'gui_random_crop_gallery_v1', 'rule_version': run['rule_version'],
                'config_fingerprint': run['config_fingerprint'], 'dataset_root': str(root),
                'seed': seed, 'sampling_unit': 'viewport',
                'selection': 'frozen_prior_selection_including_rejected' if selection_manifest else 'uniform_without_replacement_from_published_accepted_viewports',
                'original_selection_manifest': str(Path(selection_manifest).resolve()) if selection_manifest else None,
                'population_size': len(population), 'population_ids_sha256': hashlib.sha256('\n'.join(population).encode()).hexdigest(),
                'requested_count': count, 'rendered_count': len(records), 'failures': failures,
                'summary': {'unique_pages': len({r['page_id'] for r in records}),
                            'observation_statuses': dict(Counter(r['status'] for r in records)),
                            'viewports': dict(Counter(r['viewport'] for r in records)),
                            'split_viewports': sum(bool(r['cut_lines']) for r in records),
                            'whole_image_viewports': sum(not r['cut_lines'] for r in records),
                            'crop_views': sum(len(r['crops']) for r in records),
                            'published_crop_views': sum(c['status'] == 'accepted' for r in records for c in r['crops']),
                            'final_global_targets': sum(r['final_target_count'] for r in records),
                            'published_unique_targets': sum(r['published_unique_target_count'] for r in records if r['status']=='accepted')},
                'samples': records}
    write_json(output/'manifest.json', manifest)
    if records:
        _overview(output, records)
        _write_html(output, manifest)
    checks = []
    for record in records:
        directory = output/record['directory']
        w, h = record['original_size']['width'], record['original_size']['height']
        for name in ['original.jpg', 'source-boxes.jpg', 'final-boxes.jpg']:
            with Image.open(directory/name) as image:
                if image.size != (w, h):
                    checks.append(f"{record['observation_id']}/{name}: incorrect native size")
        for crop in record['crops']:
            for key in ['boxes_image', 'audit_image']:
                with Image.open(directory/crop[key]) as image:
                    if list(image.size) != crop['image_size']:
                        checks.append(f"{crop['crop_id']}: incorrect crop raster size")
    verification = {'passed': not checks and not failures and len(records) == count,
                    'samples': len(records), 'crop_views': manifest['summary']['crop_views'],
                    'all_native_sizes_verified': not checks, 'global_local_transforms_checked_during_render': True,
                    'failures': failures, 'errors': checks}
    if selection_manifest:
        verification['frozen_selection_preserved'] = [r['observation_id'] for r in records] == chosen
    write_json(output/'verification.json', verification)
    selection_text = f"沿用原随机样本和编号，不替换被剔除项；本版状态：{manifest['summary']['observation_statuses']}。" if selection_manifest else f"范围：{len(population)}个最终可用视口；抽中：{len(records)}。"
    lines = [f'# 随机{count}例整图与裁片对照', '', f"随机种子：{seed}；{selection_text}", '',
             f'[打开交互式离线图库](index.html) · [{count}例总览](overview.jpg) · [抽样清单](manifest.json)', '',
             '| 编号 | 视口 | 裁片数 | 原始候选→最终目标 | 对照图 |', '|---|---|---:|---:|---|']
    lines.extend(f"| {r['number']:02d} | {r['observation_id']} | {len(r['crops'])} | {r['source_detection_count']}→{r['final_target_count']} | [整组对照]({r['directory']}/comparison.jpg) |" for r in records)
    lines.extend(['', '整图与各实际裁片均提供原分辨率JPG；网页中可切换原始候选框、最终框和无框原图，切换ignore或展开图片高度。', '',
                  '这是可视化导出，不修改训练索引、标签或源截图。不同视口不建立语义对象对应关系。'])
    (output/'README.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    if archive and verification['passed']:
        archive_path = output.with_suffix('.zip')
        if archive_path.exists():
            raise ValueError(f'Archive already exists: {archive_path}')
        with ZipFile(archive_path, 'w', compression=ZIP_STORED) as bundle:
            for path in sorted(output.rglob('*')):
                if path.is_file():
                    bundle.write(path, f'{output.name}/{path.relative_to(output)}')
    if not verification['passed']:
        raise ValueError('Gallery export incomplete; inspect verification.json; failed samples were not silently replaced')
    return {'output': str(output), 'index': str(output/'index.html'), **manifest['summary'], 'verification_passed': True}
