"""Project validated component results into self-contained page/image/label packing input."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil

from scripts.offline_postprocess.infrastructure import read_jsonl, sha256_file, write_json
from scripts.offline_postprocess.domain import graph_depths
from scripts.webdataset.provenance import read_provenance, sample_provenance

CONTRACT = 'gui_component_pack_input_v1'
NODE_FIELDS = ('global_node_id', 'global_parent_id', 'node_id', 'parent_id',
               'layer', 'derived_depth', 'bbox', 'struct', 'subtype', 'kind', 'semantic_role',
               'text_role', 'detection_eligible', 'pixel_visibility', 'occlusion')
CROP_FIELDS = ('crop_id', 'core_bbox', 'crop_bbox', 'local_to_global', 'targets', 'ignore_regions',
               'target_union_coverage', 'detail_training_eligible', 'detail_training_discarded',
               'detail_training_reason_codes', 'box_pixel_evidence', 'crop_pixel_valid')
SPLIT_FIELDS = ('axis', 'cut_lines_original_px', 'cut_lines_normalized', 'needs_split',
                'height_constraints_px', 'resize', 'label_status', 'supervision_eligible')


def prepare_pack_input(index_path, output, workers=8, resume=False):
    """Copy accepted full PNGs and compact xyxy labels; publish pack index last, never overwrite.

    Whole-page nodes use original image pixels; crop targets/ignore use crop-local
    pixels. No segmentation is rerun and ineligible split candidates are not exported.
    Images are independent copies, not writable links to original evidence.
    """
    index_path, output = Path(index_path).resolve(), Path(output).resolve()
    index = json.loads(index_path.read_text())
    if (index.get('schema') != 'gui_component_dataset_index_v1'
            or index.get('stage') != 'final' or index.get('publication_status') != 'validated'):
        raise ValueError('A validated component final index is required')
    if not 1 <= workers <= 64:
        raise ValueError('workers must be 1..64')
    if not index.get('final_layer_projection') or not index.get('detail_training_selection'):
        if output.exists() and any(output.iterdir()) and not resume:
            raise ValueError('Output must be empty; completed exports are never overwritten')
        from scripts.pipeline.layer_projection import project_final_layers
        from scripts.pipeline.detail_selection import select_detail_crops
        threshold = index.get('detail_training_selection',{}).get('minimum_target_union_coverage',0.2)
        selected_index = index_path
        if not index.get('final_layer_projection'):
            projected = project_final_layers(index_path,output.with_name(output.name+'-layers'),resume, min(workers,32))
            selected_index = Path(projected['final_index'])
        selected = select_detail_crops(selected_index,output.with_name(output.name+'-detail-selection'),threshold,resume)
        return prepare_pack_input(selected['final_index'],output,workers,resume)
    for root in (index_path.parent, Path(index['source_root']).resolve()):
        if output == root or output in root.parents or root in output.parents:
            raise ValueError('Output must be separate from source and processing directories')
    for artifact in index['artifacts'].values():
        if sha256_file(artifact['path']) != artifact['sha256']:
            raise ValueError('Changed processing artifact: '+artifact['path'])
    validation = json.loads(Path(index['artifacts']['validation']['path']).read_text())
    if not validation.get('passed'):
        raise ValueError('Independent validation did not pass')
    if output.exists() and any(output.iterdir()):
        if resume and (output/'pack-index.json').is_file() and (output/'manifest.json').is_file():
            previous = json.loads((output/'manifest.json').read_text())
            packed = json.loads((output/'pack-index.json').read_text())
            if (previous.get('status') == 'completed'
                    and previous.get('source_index_sha256') == sha256_file(index_path)
                    and packed.get('label_contract') == CONTRACT):
                for page in packed['pages']:
                    for record in page['viewports'].values():
                        for key in ('image', 'hierarchy'):
                            file = (output/record[key]).resolve()
                            if not file.is_relative_to(output) or sha256_file(file) != record[key+'_sha256']:
                                raise ValueError('Prepared file changed; choose a new output')
                return {**previous, 'resumed': True, 'output': str(output),
                        'pack_index': str(output/'pack-index.json')}
        raise ValueError('Output must be empty or a matching completed --resume export')
    accepted = {o['observation_id']: o for o in index['observations'] if o['status'] == 'accepted'}
    if not accepted:
        raise ValueError('No accepted observations')
    crops = defaultdict(list)
    crop_source = index['artifacts'].get('detail_crop_manifest', index['artifacts']['detection_crops'])
    for crop in read_jsonl(crop_source['path']):
        oid = crop['observation_id']
        if oid not in accepted or crop['status'] != 'accepted':
            raise ValueError('Unaccepted crop in published manifest')
        value = {k: crop[k] for k in CROP_FIELDS if k in crop}
        ignore_fields = {'bbox','reason','global_node_id','global_parent_id','struct','subtype','semantic_role',
                         'pixel_visibility','occlusion','geometric_visibility','owner_core_index','detection_eligible'}
        value['ignore_regions'] = [{k:v for k,v in region.items() if k in ignore_fields} for region in value['ignore_regions']]
        crops[oid].append(value)
    splits = {}
    for split in read_jsonl(index['artifacts']['split_labels']['path']):
        oid = split['observation_id']
        if oid not in accepted or not split['supervision_eligible'] or oid in splits:
            raise ValueError('Invalid or duplicate published split record')
        splits[oid] = {k: split[k] for k in SPLIT_FIELDS}
    provenance, _ = read_provenance(Path(index['source_root']))
    jobs = []
    for oid, observation in sorted(accepted.items()):
        if (Path(oid).is_absolute() or '..' in Path(oid).parts
                or oid != observation['page_id']+'/'+observation['viewport']):
            raise ValueError('Unsafe observation ID')
        meta = sample_provenance(Path(observation['source_hierarchy']), provenance)
        if not meta.get('url'):
            raise ValueError('Missing collected URL: '+oid)
        eligible_count = sum(c.get('detail_training_eligible', True) for c in crops[oid])
        if (eligible_count != observation['published_crops']
                or not crops[oid] and not index.get('detail_training_selection')):
            raise ValueError('Missing published crops: '+oid)
        if (oid in splits) != observation['split_supervision_eligible']:
            raise ValueError('Split eligibility mismatch: '+oid)
        jobs.append((oid, observation, meta))
    output.mkdir(parents=True, exist_ok=True)

    def export_one(job):
        oid, observation, meta = job
        graph_path = Path(observation['hierarchy'])
        if sha256_file(graph_path) != observation['hierarchy_sha256']:
            raise ValueError('Changed processed hierarchy: '+oid)
        graph = json.loads(graph_path.read_text())
        depths = graph_depths(graph['nodes'])
        if any(depth>=5 for depth in depths.values()) or any(n.get('layer')!=depths[n['node_id']] for n in graph['nodes']):
            raise ValueError('Final export requires true L1-L5 nodes: '+oid)
        relative = Path('pages')/oid
        directory = output/relative
        directory.mkdir(parents=True, exist_ok=True)
        image = directory/'full.png'
        shutil.copyfile(observation['source_image'], image)
        if sha256_file(image) != observation['image_sha256']:
            raise ValueError('Image checksum mismatch: '+oid)
        label = {'schema': 'gui_processed_page_v1', 'rule_version': graph['rule_version'],
                 'annotation_status': graph['annotation_status'], 'page_id': observation['page_id'],
                 'group_id': observation['group_id'], 'viewport': observation['viewport'],
                 'url': meta['url'], 'capture': {**graph['capture'], 'image': 'full.png', 'url': meta['url']},
                 'bbox_format': 'xyxy', 'coordinate_space': 'original_image_css_pixels',
                 'crop_coordinate_space': 'crop_local_pixels',
                 'final_layer_projection': index['final_layer_projection'],
                 'nodes': [{k: n[k] for k in NODE_FIELDS if k in n} for n in graph['nodes']],
                 'detection_target_ids': graph['detection_target_ids'],
                 'crops': crops[oid], 'split_supervision_eligible': oid in splits,
                 'split_label': splits.get(oid)}
        if index.get('detail_training_selection'):
            label.update(detail_training_selection=index['detail_training_selection'],
                         detail_training_crop_ids=[c['crop_id'] for c in crops[oid] if c['detail_training_eligible']],
                         discarded_detail_crop_ids=[c['crop_id'] for c in crops[oid] if c['detail_training_discarded']],
                         detail_training_eligible=any(c['detail_training_eligible'] for c in crops[oid]))
        label_path = directory/'hierarchy.json'
        write_json(label_path, label)
        rel_label = (relative/'hierarchy.json').as_posix()
        return oid, meta, {'image': (relative/'full.png').as_posix(), 'hierarchy': rel_label,
                          'original_hierarchy': rel_label, 'hierarchy_side': 'original',
                          'hierarchy_sha256': sha256_file(label_path),
                          'image_sha256': observation['image_sha256']}

    pages = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for oid, meta, record in pool.map(export_one, jobs):
            page_id, viewport = oid.split('/')
            page = pages.setdefault(page_id, {'page_id': page_id, 'run_id': 'prepared',
                'page_directory': 'pages/'+page_id, 'status': 'valid',
                **{k: meta.get(k) for k in ('url', 'entry_url', 'source_id', 'title', 'category',
                                           'category_path', 'url_source')}, 'viewports': {}})
            if page['url'] != meta['url']:
                raise ValueError('Inconsistent URLs within page: '+page_id)
            page['viewports'][viewport] = record
    pack_index = {'schema': 'responsive_ui_pipeline_index_v1', 'stage': 'final',
                  'label_contract': CONTRACT, 'runs': [{'id': 'prepared', 'root': '.'}],
                  'required_viewports': sorted({o['viewport'] for o in accepted.values()}),
                  'pages': list(pages.values())}
    trainable_count = sum(c.get('detail_training_eligible', True) for cs in crops.values() for c in cs)
    discarded_count = sum(c.get('detail_training_discarded', False) for cs in crops.values() for c in cs)
    pack_index['detail_training_selection'] = index.get('detail_training_selection')
    pack_index['final_layer_projection'] = index['final_layer_projection']
    pack_index['detail_training_counts'] = {'trainable_crops':trainable_count, 'discarded_detail_crops':discarded_count}
    summary = {'schema': CONTRACT, 'status': 'completed', 'pages': len(pages),
               'observations': len(accepted), 'trainable_crops': trainable_count,
               'discarded_detail_crops': discarded_count,
               'detail_training_selection': index.get('detail_training_selection'),
               'split_labels': len(splits), 'missing_url': 0,
               'excluded_observations': len(index['observations'])-len(accepted),
               'source_index_sha256': sha256_file(index_path),
               'image_storage': 'independent_byte_identical_copies',
               'annotation_rule_version': index['rule_version']}
    summary['final_layer_projection'] = index['final_layer_projection']
    write_json(output/'manifest.json', summary)
    (output/'README.md').write_text(
        '# Prepared dataset\n\nOnly accepted viewports are included. Each `pages/<page>/<viewport>/` '
        'contains `full.png` and `hierarchy.json` (URL, compact component graph, usable crop labels '
        'and eligible split label). Images are original full screenshots; crop images are extracted '
        'using `crop_bbox`. Boxes use xyxy; nodes use whole-image coordinates, crop targets and '
        'ignore regions use crop-local coordinates. Read only crops with `detail_training_eligible=true` '
        'when this flag is present; discarded crops remain for audit, and do not invalidate sibling '
        'crops, the viewport or page-cut supervision. Split candidates without eligibility are null. '
        'Labels remain rule-generated, not manually verified. Keep page/site groups in one dataset split.\n\n'
        'The portable `pack-index.json` is the input to `scripts/pipeline/cli.py pack`. '
        'Do not run legacy depth filtering on these labels. Rejected/review viewports, audit logs, '
        'diagnostics and source absolute paths are excluded.\n', encoding='utf-8')
    # The pack-ready index is the completion marker and is intentionally written last.
    write_json(output/'pack-index.json', pack_index)
    return {**summary, 'output': str(output), 'pack_index': str(output/'pack-index.json')}
