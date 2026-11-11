"""Export V6 core-height crop images with core-local JSON labels, grouped by viewport."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from PIL import Image

from scripts.offline_postprocess.domain import area, contains, intersection
from scripts.offline_postprocess.infrastructure import read_jsonl, sha256_file, write_json
from scripts.offline_postprocess.slicing_statistics import rectangle_union_area
from scripts.offline_postprocess.statistics import hundred_px_histogram
from scripts.webdataset.provenance import read_provenance, sample_provenance

CONTRACT = 'gui_component_crop_export_v6'


def _pixel_box(box, width, height):
    """Clamp a CSS xyxy box to integer image pixels; V6 image bytes use these exact bounds."""
    x1 = max(0, min(width, int(round(box[0]))))
    y1 = max(0, min(height, int(round(box[1]))))
    x2 = max(0, min(width, int(round(box[2]))))
    y2 = max(0, min(height, int(round(box[3]))))
    if x2 <= x1 or y2 <= y1:
        raise ValueError('Empty V6 core crop')
    return [x1, y1, x2, y2]


def _clip_target(target, crop_box, core_box):
    """Convert one crop-local target to core-local xyxy, clipping and flagging partial views."""
    x, y = crop_box[:2]
    global_box = [target['bbox'][0]+x, target['bbox'][1]+y, target['bbox'][2]+x, target['bbox'][3]+y]
    clipped = intersection(global_box, core_box)
    if area(clipped) <= 0:
        return None
    value = dict(target)
    value['bbox'] = [clipped[0]-core_box[0], clipped[1]-core_box[1],
                     clipped[2]-core_box[0], clipped[3]-core_box[1]]
    value['geometric_visibility'] = 'complete' if contains(core_box, global_box) else 'partial'
    return value


def _clip_ignore(ignore, crop_box, core_box):
    """Clip an ignore region to the V6 core crop, preserving its original reason and IDs."""
    x, y = crop_box[:2]
    global_box = [ignore['bbox'][0]+x, ignore['bbox'][1]+y, ignore['bbox'][2]+x, ignore['bbox'][3]+y]
    clipped = intersection(global_box, core_box)
    if area(clipped) <= 0:
        return None
    value = dict(ignore)
    value['bbox'] = [clipped[0]-core_box[0], clipped[1]-core_box[1],
                     clipped[2]-core_box[0], clipped[3]-core_box[1]]
    return value


def _crop_order(crop, fallback):
    """Use the stable crop suffix when numeric, otherwise keep source manifest order."""
    suffix = str(crop.get('crop_id', '')).rsplit('/', 1)[-1]
    return int(suffix) if suffix.isdigit() else fallback


def export_core_crops_v6(index_path, output, workers=8, resume=False):
    """Materialize one core-height PNG and core-local JSON per accepted crop.

    Output layout is `pages/<viewport>/<page_id>_<crop_order>.png` plus a same-stem
    `.json`. Full screenshots are not copied. The JSON preserves source crop geometry,
    target/ignore fields and detail-selection flags while shifting coordinates to the
    core crop. Source labels, images and pipeline indexes are never modified.
    """
    index_path, output = Path(index_path).resolve(), Path(output).resolve()
    index = json.loads(index_path.read_text())
    if index.get('schema') != 'gui_component_dataset_index_v1' or index.get('publication_status') != 'validated':
        raise ValueError('A validated component final index is required')
    if not index.get('final_layer_projection') or index['final_layer_projection'].get('max_layers') != 5:
        raise ValueError('V6 export requires the final L1-L5 index')
    if not 1 <= workers <= 32:
        raise ValueError('workers must be 1..32')
    manifest_path = index['artifacts'].get('detail_crop_manifest', index['artifacts']['detection_crops'])
    if sha256_file(manifest_path['path']) != manifest_path['sha256']:
        raise ValueError('Changed crop manifest')
    if output.exists() and any(output.iterdir()):
        marker = output/'manifest.json'
        if resume and marker.is_file():
            previous = json.loads(marker.read_text())
            if previous.get('contract') == CONTRACT and previous.get('source_index_sha256') == sha256_file(index_path):
                return {**previous, 'resumed': True, 'output': str(output)}
        raise ValueError('V6 output must be empty or matching --resume output')
    output.mkdir(parents=True, exist_ok=True)
    accepted = {o['observation_id']:o for o in index['observations'] if o['status']=='accepted'}
    crops_by = defaultdict(list)
    for crop in read_jsonl(manifest_path['path']):
        oid = crop['observation_id']
        if oid not in accepted or crop.get('status') != 'accepted':
            continue
        crops_by[oid].append(crop)
    provenance, _ = read_provenance(Path(index['source_root']))

    def export_one(observation):
        oid = observation['observation_id']
        meta = sample_provenance(Path(observation['source_hierarchy']), provenance)
        url = meta.get('url') or observation.get('url')
        title = meta.get('title') or observation.get('title')
        graph = json.loads(Path(observation['hierarchy']).read_text())
        records = []
        with Image.open(observation['source_image']) as opened:
            with opened.convert('RGB') as original:
                previous_bottom = None
                for fallback, crop in enumerate(sorted(crops_by[oid], key=lambda c: str(c.get('crop_id', '')))):
                    order = _crop_order(crop, fallback)
                    core = _pixel_box(crop['core_bbox'], original.width, original.height)
                    if previous_bottom is not None and core[1] < previous_bottom:
                        raise ValueError(f'V6 core crops overlap: {oid}/{order}')
                    previous_bottom = core[3]
                    crop_box = crop['crop_bbox']
                    directory = output/'pages'/observation['viewport']
                    directory.mkdir(parents=True, exist_ok=True)
                    stem = f"{observation['page_id']}_{order:04d}"
                    image_path, label_path = directory/f'{stem}.png', directory/f'{stem}.json'
                    with original.crop(tuple(core)) as image:
                        image.save(image_path, format='PNG', optimize=False)
                        image_size = [image.width, image.height]
                    targets = [v for t in crop.get('targets', []) if (v := _clip_target(t, crop_box, core)) is not None]
                    ignores = [v for i in crop.get('ignore_regions', []) if (v := _clip_ignore(i, crop_box, core)) is not None]
                    coverage = min(1.0, rectangle_union_area([t['bbox'] for t in targets])/max(1, area([0, 0, *image_size]))) if targets else None
                    label = {
                        'schema': 'gui_component_crop_v6',
                        'contract': CONTRACT,
                        'sample_folder': observation['page_id'],
                        'page_id': observation['page_id'],
                        'viewport': observation['viewport'],
                        'observation_id': oid,
                        'crop_id': crop['crop_id'],
                        'crop_order': order,
                        'image': f'{stem}.png',
                        'image_size': image_size,
                        'core_width': image_size[0],
                        'core_height': image_size[1],
                        'source_image_size': [original.width, original.height],
                        'bbox_format': 'xyxy',
                        'coordinate_space': 'core_crop_local_pixels',
                        'source_coordinate_space': 'original_image_css_pixels',
                        'core_bbox': list(crop['core_bbox']),
                        'core_pixel_bbox': core,
                        'context_included': False,
                        'crop_content_unique': True,
                        'url': url, 'title': title,
                        'targets': targets, 'ignore_regions': ignores,
                        'target_count': len(targets),
                        'target_union_coverage': coverage,
                        'detail_training_eligible': crop.get('detail_training_eligible'),
                        'detail_training_discarded': crop.get('detail_training_discarded'),
                        'detail_training_reason_codes': crop.get('detail_training_reason_codes', []),
                        'detail_training_decision_basis': 'V5 crop_bbox target_union_coverage',
                        'split_supervision_eligible': observation.get('split_supervision_eligible', False),
                        'split_label': graph.get('split_label'),
                        'final_layer_projection': index['final_layer_projection'],
                        'rule_version': graph.get('rule_version'),
                        'annotation_status': graph.get('annotation_status'),
                    }
                    write_json(label_path, label)
                    records.append({
                        'page_id': observation['page_id'], 'viewport': observation['viewport'],
                        'observation_id': oid, 'crop_id': crop['crop_id'], 'crop_order': order,
                        'image': image_path.relative_to(output).as_posix(),
                        'label': label_path.relative_to(output).as_posix(),
                        'image_sha256': sha256_file(image_path), 'label_sha256': sha256_file(label_path),
                        'core_width': image_size[0], 'core_height': image_size[1],
                        'target_count': len(targets), 'partial_targets': sum(t['geometric_visibility'] == 'partial' for t in targets),
                        'ignore_count': len(ignores),
                        'detail_training_eligible': crop.get('detail_training_eligible'),
                        'detail_training_discarded': crop.get('detail_training_discarded'),
                    })
        return records

    crops = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for records in pool.map(export_one, sorted(accepted.values(), key=lambda o: o['observation_id'])):
            crops.extend(records)
    heights = sorted(r['core_height'] for r in crops)
    targets = [r['target_count'] for r in crops]
    def percentile(values, fraction):
        return values[min(len(values)-1, int(len(values)*fraction))] if values else None
    stats = {
        'crops': len(crops), 'observations': len(accepted), 'pages': len({r['page_id'] for r in crops}),
        'trainable_crops': sum(r['detail_training_eligible'] is True for r in crops),
        'discarded_crops': sum(r['detail_training_discarded'] is True for r in crops),
        'core_height': {'min':min(heights,default=None), 'median':percentile(heights,.5),
                        'p90':percentile(heights,.9), 'p99':percentile(heights,.99), 'max':max(heights,default=None)},
        'core_height_histogram_100px': hundred_px_histogram(heights),
        'targets': {'total':sum(targets), 'mean':sum(targets)/len(targets) if targets else None,
                    'median':percentile(sorted(targets),.5), 'max':max(targets,default=None),
                    'zero_target_crops':sum(n==0 for n in targets),
                    'partial':sum(r['partial_targets'] for r in crops),
                    'ignore_regions':sum(r['ignore_count'] for r in crops)},
        'viewports': {},
    }
    for viewport in sorted({r['viewport'] for r in crops}):
        rows = [r for r in crops if r['viewport']==viewport]
        stats['viewports'][viewport] = {
            'crops':len(rows), 'trainable':sum(r['detail_training_eligible'] is True for r in rows),
            'discarded':sum(r['detail_training_discarded'] is True for r in rows),
            'targets':sum(r['target_count'] for r in rows),
            'widths':dict(sorted(Counter(r['core_width'] for r in rows).items())),
        }
    summary = {
        'schema': CONTRACT, 'contract': CONTRACT, 'status': 'completed',
        'layout': 'pages/<viewport>/<page_id>_<crop_order>.png|.json',
        'image_storage': 'core_height_crop_png', 'core_crop': True,
        'context_included': False, 'crop_content_unique': True,
        'core_crops_non_overlapping': True,
        'label_coordinate_space': 'core_crop_local_pixels',
        'observations': len(accepted), 'crops': len(crops),
        'viewports': dict(sorted(Counter(r['viewport'] for r in crops).items())),
        'pages': len({r['page_id'] for r in crops}),
        'trainable_crops': sum(r['detail_training_eligible'] is True for r in crops),
        'discarded_crops': sum(r['detail_training_discarded'] is True for r in crops),
        'source_index_sha256': sha256_file(index_path),
        'detail_training_selection': index.get('detail_training_selection'),
        'final_layer_projection': index['final_layer_projection'],
        'statistics': stats,
    }
    write_json(output/'manifest.json', summary)
    write_json(output/'crops-index.json', {'schema': CONTRACT, 'crops': crops})
    (output/'reports').mkdir(parents=True, exist_ok=True)
    write_json(output/'reports/statistics.json', stats)
    lines = ['# V6 core-crop statistics', '',
             f"crops: {stats['crops']} | observations: {stats['observations']} | pages: {stats['pages']}",
             f"trainable: {stats['trainable_crops']} | discarded: {stats['discarded_crops']}", '',
             '| viewport | crops | trainable | discarded | targets | widths |', '|---|---:|---:|---:|---:|---|']
    for viewport, row in stats['viewports'].items():
        lines.append(f"| {viewport} | {row['crops']} | {row['trainable']} | {row['discarded']} | {row['targets']} | {row['widths']} |")
    lines += ['', '## Core height (100px bins)', '', '| interval | crops |', '|---|---:|']
    lines += [f'| {k} | {v} |' for k,v in stats['core_height_histogram_100px'].items()]
    lines += ['', f"core height min/median/p90/p99/max: `{stats['core_height']}`",
              f"targets: `{stats['targets']}`", '',
              'Core crops are disjoint; JSON labels are core-local and do not record context crop size.']
    (output/'reports/statistics.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    (output/'README.md').write_text(
        '# V6 core-crop export\n\n'
        'Each accepted crop is materialized at its `core_bbox` height as '
        '`pages/<viewport>/<page_id>_<crop_order>.png`. The same-stem `.json` contains '
        'the URL, core geometry and V5 targets/ignore regions in core-crop-local xyxy; '
        'the context `crop_bbox` is deliberately not recorded, so adjacent core crops '
        'do not duplicate context content. '
        'Targets crossing the core boundary are marked `geometric_visibility=partial`. '
        'Only accepted viewport crops are exported; full-page screenshots are not copied. '
        '`detail_training_eligible`/`detail_training_discarded` preserve the preceding '
        'detail-filter decision. `crops-index.json` lists every image/label pair with hashes. '
        'Labels remain rule-generated candidates, not manually verified ground truth.\n', encoding='utf-8')
    return {**summary, 'output': str(output)}
