"""Bundle a complete local dataset catalogue into one offline random-sampling HTML viewer."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import base64
import gzip
import json
import os
from pathlib import Path
from urllib.parse import quote
from .infrastructure import read_jsonl, sha256_file, write_json
from scripts.webdataset.provenance import read_provenance, sample_provenance


def build_dataset_viewer(index_path, output, workers=8):
    """Embed lazy per-viewport annotations in HTML; reference source PNGs with relative file URLs.

    Whole-page/source boxes are original-image xyxy; crop targets/ignores are crop-local.
    Detection targets carry their final L1-L5 layer; the HTML can show one layer or any
    combination and restricts random sampling to observations containing a chosen layer.
    No source labels/images or published training manifests are modified. This file
    opens directly without a web server, network calls or browser filesystem grants.
    """
    index_path, output = Path(index_path).resolve(), Path(output).resolve()
    if not 1 <= workers <= 32:
        raise ValueError('workers must be 1..32')
    index = json.loads(index_path.read_text())
    if index.get('schema') != 'gui_component_dataset_index_v1' or index.get('publication_status') != 'validated':
        raise ValueError('A validated component index is required')
    if not index.get('final_layer_projection') or index['final_layer_projection'].get('max_layers') != 5:
        raise ValueError('Use the final L1-L5 selection index; the full generation index is audit-only')
    for artifact in index['artifacts'].values():
        if sha256_file(artifact['path']) != artifact['sha256']:
            raise ValueError('Changed artifact: '+artifact['path'])
    selected = defaultdict(dict)
    source = index['artifacts'].get('detail_crop_manifest', index['artifacts']['detection_crops'])
    for crop in read_jsonl(source['path']):
        selected[crop['observation_id']][crop['crop_id']] = {
            key:crop[key] for key in ('target_union_coverage', 'detail_training_eligible',
                                      'detail_training_discarded', 'detail_training_reason_codes', 'box_pixel_evidence', 'crop_pixel_valid') if key in crop}
    provenance, _ = read_provenance(Path(index['source_root']))

    def compact(box, identifier='', kind=''):
        return [*box, identifier, kind]

    def pack(observation):
        oid = observation['observation_id']
        meta = sample_provenance(Path(observation['source_hierarchy']), provenance)
        entry = {'id':oid, 'page':observation['page_id'], 'viewport':observation['viewport'],
                 'status':observation['status'], 'reasons':observation['reason_codes'],
                 'url':meta.get('url'), 'title':meta.get('title'),
                 'trainable':observation.get('published_crops',0),
                 'discarded':observation.get('discarded_detail_crops',0),
                 'split':observation.get('split_supervision_eligible',False)}
        try:
            audit = json.loads(Path(observation['audit']).read_text())
            raw = json.loads(Path(observation['source_hierarchy']).read_text())
            graph = json.loads(Path(observation['hierarchy']).read_text()) if observation.get('hierarchy') else {'nodes':[]}
            source_boxes = []
            for node in raw.get('nodes',[]):
                if node.get('disposition') == 'detection':
                    x,y,w,h = node['bbox']
                    source_boxes.append(compact([x,y,x+w,y+h],node.get('node_id',''),node.get('struct','')))
            def node_layer(node):
                depth = node.get('derived_depth')
                if depth is None:
                    depth = node.get('layer', 0)
                return int(depth)+1
            node_layers = {n['global_node_id']:node_layer(n) for n in graph['nodes'] if n.get('detection_eligible')}
            def target_label(target):
                layer = node_layers.get(target['global_node_id'])
                return (f'L{layer} ' if layer else '')+str(target.get('struct')or'')+'/'+str(target.get('subtype')or'')
            final_boxes = [compact(n['bbox'],n['global_node_id'],f"L{node_layers[n['global_node_id']]} "+str(n.get('struct')or'')+'/'+str(n.get('subtype')or''))
                           for n in graph['nodes'] if n.get('detection_eligible')]
            layer_counts = defaultdict(int)
            for layer in node_layers.values():
                layer_counts[layer] += 1
            crops = []
            for crop in audit['crops']:
                flags = selected[oid].get(crop['crop_id'],{})
                eligible = observation['status']=='accepted' and crop['status']=='accepted' and flags.get('detail_training_eligible',True)
                crops.append({'id':crop['crop_id'], 'box':crop['crop_bbox'], 'core':crop['core_bbox'],
                    'targets':[compact(t['bbox'],t['global_node_id'],target_label(t)) for t in crop['targets']],
                    'ignores':[compact(t['bbox'],t.get('global_node_id',''),t.get('reason','')) for t in crop['ignore_regions']],
                    'eligible':eligible, 'discarded':flags.get('detail_training_discarded',False),
                    'coverage':flags.get('target_union_coverage'), 'pixels':flags.get('box_pixel_evidence'),
                    'reasons':flags.get('detail_training_reason_codes') or crop.get('reason_codes',[]),
                    'geometry_status':crop['status']})
            entry['candidate_crops'] = len(crops)
            entry['layers'] = sorted(layer_counts)
            entry['layer_counts'] = {str(layer):layer_counts[layer] for layer in sorted(layer_counts)}
            image = quote(os.path.relpath(observation['source_image'],output.parent),safe='/')
            data = {'image':image, 'size':observation['original_size'], 'source':source_boxes,
                    'final':final_boxes, 'cuts':(audit.get('split_label') or {}).get('cut_lines_original_px',[]), 'crops':crops}
        except (OSError, ValueError, KeyError, TypeError) as error:
            entry['load_error'] = f'{type(error).__name__}: {error}'
            data = {'error':entry['load_error']}
        blob = gzip.compress(json.dumps(data,ensure_ascii=False,separators=(',',':')).encode(),compresslevel=6,mtime=0)
        return entry, base64.b64encode(blob).decode('ascii')

    catalogue, payloads = [], {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for entry, payload in pool.map(pack,index['observations']):
            catalogue.append(entry)
            payloads[entry['id']] = payload
    stats_path = index_path.parent/'reports/layer-statistics.json'
    layer_stats = json.loads(stats_path.read_text()) if stats_path.is_file() else None
    if not layer_stats or layer_stats.get('source_index_sha256') != sha256_file(index_path):
        from .layer_statistics import compute_layer_statistics
        layer_stats = compute_layer_statistics(index_path,workers=workers)
    bundle = {'catalogue':catalogue, 'payloads':payloads, 'rule_version':index['rule_version'],
              'final_layer_projection':index.get('final_layer_projection'),
              'layer_statistics':layer_stats,
              'selection':index.get('detail_training_selection'), 'statuses':index['statuses'],
              'index_sha256':sha256_file(index_path)}
    encoded = json.dumps(bundle,ensure_ascii=False,separators=(',',':')).replace('<',chr(92)+'u003c')
    template = Path(__file__).with_name('dataset_viewer.html').read_text()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(template.replace('__DATASET_BUNDLE__',encoded),encoding='utf-8')
    report = {'output':str(output), 'observations':len(catalogue), 'statuses':index['statuses'],
              'load_errors':[e for e in catalogue if e.get('load_error')], 'bytes':output.stat().st_size,
              'source_index_sha256':bundle['index_sha256'], 'opens_without_server':True}
    write_json(output.with_suffix('.manifest.json'),report)
    return report
