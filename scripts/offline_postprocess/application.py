"""Concurrent offline orchestration, provenance, publication and independent page failure handling."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import threading
import time
from PIL import Image
import numpy as np
from .config import fingerprint
from .domain import VERSION, normalize, xyxy, area, intersection
from .cleanup import clean_nodes
from .quality import scope_evidence, geometry_evidence
from .pixels import inspect_image
from .slicing import plan_views
from .regions import propose_regions
from .infrastructure import discover, sha256_file, write_json, write_jsonl
from scripts.page_filter.stages import pre_filter_source, post_filter_crops, color_evidence


def process_one(item, output, config, scope_rejections, pixel_gate, resume=False, stop_after='final-filter'):
    """Process one viewport in isolation; publish source hashes, graph, trace and crop records atomically."""
    observation_id = item['observation_id']
    directory = output/'observations'/observation_id
    result_path = directory/'result.json'
    config_hash = fingerprint(config)
    quality = {'scope': 'observation', 'observation_id': observation_id, 'page_id': item['page_id'],
               'viewport': item['viewport'], 'group_id': item['page_id'], 'status': 'accepted',
               'reason_codes': [], 'evidence': {}, 'missing_evidence': ['pixel_occlusion', 'page_stability', 'semantic_error_templates'],
               'rule_version': VERSION, 'config_fingerprint': config_hash,
               'source_hierarchy': item['hierarchy'], 'source_image': item['image']}
    result = {'quality': quality, 'crops': [], 'split_label': None, 'counts': {}, 'operations': [], 'partition': {}}
    source_count, source_nodes_count = 0, 0
    try:
        source_hash = sha256_file(item['hierarchy'])
        image_hash = sha256_file(item['image'])
        quality.update(source_hash=source_hash, image_sha256=image_hash)
        if resume and result_path.is_file():
            try:
                cached = json.loads(result_path.read_text())
                old = cached['quality']
                if not isinstance(old, dict):
                    raise ValueError('Malformed checkpoint quality')
            except (OSError, ValueError, KeyError, TypeError):
                cached, old = {}, {}
            if (all(old.get(key) == quality.get(key) for key in ('rule_version', 'config_fingerprint', 'source_hash', 'image_sha256'))
                    and old.get('evidence', {}).get('scope') == scope_rejections.get(item['page_id'])
                    and cached.get('completed_stage', 'final-filter') == stop_after
                    and cached.get('artifact_sha256') and (directory/'hierarchy.json').is_file()
                    and cached.get('artifact_sha256') == sha256_file(directory/'hierarchy.json')):
                return {'observation_id': observation_id, 'status': old['status'], 'resumed': True}
        source = json.loads(Path(item['hierarchy']).read_text())
        quality['source_rule_version'] = source.get('rule_version', 'unknown')
        quality['source_annotation_status'] = source.get('annotation_status', 'unknown')
        source_nodes_count = len(source.get('nodes', []))
        source_count = sum(n.get('disposition') == 'detection' for n in source.get('nodes', []))
        nodes, width, height = normalize(source, observation_id)
        quality['original_size'] = {'width': width, 'height': height}
        # Scope exclusion precedes cleanup, and applies to all viewports of the original page.
        if item['page_id'] in scope_rejections:
            quality.update(status='rejected', reason_codes=['out_of_scope_dense_table'])
            quality['evidence']['scope'] = scope_rejections[item['page_id']]
        else:
            pre_filter = pre_filter_source(source, config['pre_filter'])
            quality['evidence']['pre_filter'] = pre_filter
            if pre_filter['status'] != 'accepted':
                quality.update(status=pre_filter['status'], reason_codes=list(pre_filter['reason_codes']))
            confirmed_error = config['quality']['confirmed_error_observations'].get(observation_id)
            if confirmed_error:
                if image_hash == confirmed_error['image_sha256']:
                    quality.update(status='rejected', reason_codes=[confirmed_error['reason']], noise_tier='confirmed_error')
                    quality['evidence']['confirmed_error'] = confirmed_error
                else:
                    quality.update(status='review', reason_codes=['confirmed_error_source_changed'], noise_tier='unverified_changed_source')
            elif config['quality']['review_unconfirmed_error_page_peers'] and any(
                    key.rsplit('/', 1)[0] == item['page_id'] for key in config['quality']['confirmed_error_observations']):
                quality.update(status='review', reason_codes=['known_error_page_peer_unverified'], noise_tier='error_page_peer_pending_review')
            quality['evidence']['geometry'] = geometry_evidence(nodes, width, height, config['quality'])
            if quality['status'] == 'accepted' and quality['evidence']['geometry']['outside_target_ids']:
                quality.update(status='review', reason_codes=['severe_target_geometry'])
            if observation_id in config['quality']['suspect_observations']:
                quality.update(status='review', reason_codes=quality['reason_codes']+['annotation_alignment_suspect'], noise_tier='systematic_alignment_suspect')
            with pixel_gate:
                pixels = inspect_image(item['image'], width, height, config['pixels'])
            quality['evidence']['pixels'] = {k: v for k, v in pixels.items() if k not in {'separators', 'analysis_gray', 'analysis_rgb'}}
            if config['blank_crop_filter']['enabled'] and pixels['std'] < 2:
                page_color = color_evidence(pixels['analysis_rgb'], config['blank_crop_filter'])
                quality['evidence']['page_color'] = page_color
                if page_color['rejected']:
                    quality.update(status='rejected', reason_codes=['blank_or_solid_page'])
            result['pre_filter'] = {'status': quality['status'], 'reason_codes': list(quality['reason_codes']),
                                    'source_hash': source_hash, 'evidence': quality['evidence'].copy()}
            if quality['status'] == 'accepted' and stop_after != 'filter':
                proposals = propose_regions(nodes, width, height, pixels['analysis_gray'], config['local_regions'],
                                            config['pixels'], config['slicing']['max_height'])
                nodes, operations, cleanup_evidence = clean_nodes(nodes, width, height, config['cleanup'],
                                                                config['slicing']['max_height'], config['slicing']['trigger_height_gt'], proposals)
                result['operations'] = operations
                quality['evidence']['cleanup'] = cleanup_evidence
                quality['evidence']['local_regions'] = {'proposals': len(proposals),
                    'applied': sum(op['op'] == 'apply_local_partition' for op in operations)}
                targets = [n for n in nodes if n['detection_eligible']]
                source_ignores = []
                for ignore in source.get('ignore_regions', []):
                    if not isinstance(ignore, dict) or 'bbox' not in ignore:
                        raise ValueError('invalid_ignore_annotation')
                    source_ignores.append({'bbox': xyxy(ignore['bbox']), 'reason': 'source_ignore', 'source': ignore})
                # Pixel labels are never invented for dropped oversized backgrounds.
                # Unknown completeness is explicit; known source ignores stay local and retain positives.
                if any(n.get('incomplete_annotation_risk') for n in nodes):
                    quality['missing_evidence'].append('oversized_background_internal_annotation_completeness')
                cuts, partition, crops, missing, qualification = plan_views(
                    observation_id, width, height, targets, pixels['separators'], config['slicing'],
                    config['split_supervision'], source_ignores)
                partition['local_region_proposals'] = proposals
                result.update(crops=crops, partition=partition)
                quality['evidence']['unrepresented_target_ids'] = missing
                quality['evidence']['boundary_supervision'] = qualification
                quality.setdefault('noise_tier', 'rule_candidates_with_bounded_boundary_noise')
                for crop in crops:
                    crop.update(page_id=item['page_id'], group_id=item['page_id'], source_image=item['image'],
                                hierarchy=str(directory/'hierarchy.json'), rule_version=VERSION, config_fingerprint=config_hash)
                    top, bottom = crop['crop_bbox'][1], crop['crop_bbox'][3]
                    sample = pixels['analysis_gray'][top:bottom].astype(np.float32)
                    crop['pixel_diagnostics'] = {'std': float(sample.std()),
                        'edge_ratio': float(np.mean(np.abs(np.diff(sample, axis=0)) > 12)) if len(sample) > 1 else 0.0}
                    crop['pixel_diagnostics']['low_information_warning'] = bool(crop['pixel_diagnostics']['std'] < 2
                        and crop['pixel_diagnostics']['edge_ratio'] < .0001)
                final_filter = (post_filter_crops(pixels['analysis_rgb'], width, crops, config['blank_crop_filter'])
                                if stop_after == 'final-filter' else
                                {'checked': False, 'rejected': False, 'crops': []})
                quality['evidence']['post_filter'] = final_filter
                for crop, check in zip(crops, final_filter['crops']):
                    crop['pixel_diagnostics']['blank_or_solid_check'] = check
                result['postprocess_status'] = 'accepted'
                if final_filter['rejected']:
                    quality.update(status='rejected', reason_codes=['blank_or_solid_crop'], noise_tier='blank_or_solid_crop')
                    qualification.update(eligible=False, quality_tier='rejected_blank_crop',
                                         reason_codes=qualification['reason_codes']+['blank_or_solid_crop'])
                    for crop in crops:
                        crop['pre_quality_status'] = crop['status']
                        crop.update(status='rejected', reason_codes=crop['reason_codes']+['observation_has_blank_or_solid_crop'])
                if quality['status'] == 'accepted' and not any(c['status'] == 'accepted' for c in crops):
                    quality.update(status='review', reason_codes=['no_usable_detection_crops'])
                eligible = stop_after == 'final-filter' and quality['status'] == 'accepted' and qualification['eligible']
                scale = min(1, config['split_supervision']['max_input_width']/width, config['split_supervision']['max_input_height']/height)
                resized = [max(1, round(width*scale)), max(1, round(height*scale))]
                rows = [round(y*resized[1]/height) for y in cuts]
                if len(set(rows)) != len(rows) or any(y <= 0 or y >= resized[1] for y in rows):
                    eligible = False
                    quality['evidence']['split_resolution_insufficient'] = True
                label = {'schema': 'gui_page_cut_lines_v1', 'observation_id': observation_id,
                         'page_id': item['page_id'], 'group_id': item['page_id'], 'image': item['image'],
                         'original_size': {'width': width, 'height': height}, 'axis': 'y',
                         'cut_lines_original_px': cuts, 'cut_lines_normalized': [y/height for y in cuts],
                         'needs_split': bool(cuts), 'height_constraints_px': {
                             'trigger_above': config['slicing']['trigger_height_gt'],
                             'min': config['slicing']['min_height'], 'max': config['slicing']['max_height']},
                         'label_status': 'rule_generated_unverified', 'supervision_eligible': eligible,
                         'fallback': partition['fallback'], 'cut_object_count': partition['cut_object_count'],
                         'quality_tier': qualification['quality_tier'],
                         'supervision_reason_codes': qualification['reason_codes'],
                         'resize': {'width': resized[0], 'height': resized[1], 'scale_x': resized[0]/width,
                                    'scale_y': resized[1]/height, 'padding': [0, 0, 0, 0],
                                    'valid_bbox': [0, 0, *resized], 'interpolation': 'bilinear'},
                         'source_hierarchy_sha256': source_hash, 'rule_version': VERSION, 'config_fingerprint': config_hash}
                result['split_label'] = label
                if stop_after == 'final-filter' and quality['status'] == 'accepted' and config['slicing']['materialize_crops']:
                    with pixel_gate:
                        with Image.open(item['image']) as image:
                            for pos, crop in enumerate(crops):
                                crop_file = directory/'crops'/f'{pos:04d}.png'
                                crop_file.parent.mkdir(parents=True, exist_ok=True)
                                with image.crop(crop['crop_bbox']) as view:
                                    view.save(crop_file)
                                crop['image'] = str(crop_file)
        result.setdefault('pre_filter', {'status': quality['status'], 'reason_codes': list(quality['reason_codes'])})
        graph = {'schema': 'gui_component_graph_v1', 'rule_version': VERSION,
                 'annotation_status': 'rule_generated_unverified', 'bbox_format': 'xyxy',
                 'coordinate_space': 'original_image_css_pixels', 'observation_id': observation_id,
                 'capture': source['capture'], 'source_hash': source_hash, 'source_rule_version': source.get('rule_version'),
                 'config_fingerprint': config_hash, 'nodes': nodes,
                 'detection_target_ids': [n['global_node_id'] for n in nodes if n['detection_eligible']],
                 'source_ignore_regions': source.get('ignore_regions', []), 'source_decisions': source.get('decisions', [])}
        write_json(directory/'hierarchy.json', graph)
        result['artifact_sha256'] = sha256_file(directory/'hierarchy.json')
        result['counts'] = {'source_nodes': source_nodes_count, 'source_detection': source_count,
                            'derived_nodes': len(nodes), 'final_detection': sum(n['detection_eligible'] for n in nodes),
                            'generated_groups': sum(bool(n.get('generated')) for n in nodes),
                            'excluded_by_reason': dict(Counter(n['supervision_reason'] for n in nodes
                                                       if n['source_detection_eligible'] and not n['detection_eligible'])),
                            'activated_source_groups': sum(n['detection_eligible'] and not n['source_detection_eligible']
                                                           and not n.get('generated') for n in nodes)}
    except Exception as error:
        quality.update(status='review', reason_codes=quality['reason_codes']+[str(error) if isinstance(error, ValueError) else type(error).__name__])
        quality['evidence']['error'] = f'{type(error).__name__}: {error}'
        result.update(crops=[], split_label=None)
        result['counts'].setdefault('source_nodes', source_nodes_count)
        result['counts'].setdefault('source_detection', source_count)
    result['completed_stage'] = stop_after
    write_json(directory/'result.json', result)
    write_jsonl(directory/'operations.jsonl', result['operations'])
    write_json(directory/'partitions.json', result['partition'])
    return {'observation_id': observation_id, 'status': quality['status'], 'resumed': False}


def run_dataset(root, output, config, resume=False, limit=0, page_ids=None, stop_after='final-filter'):
    """Process all discovered viewports concurrently and publish deterministic manifests after completion."""
    if stop_after not in ('filter', 'postprocess', 'final-filter'):
        raise ValueError('Unknown processing stop stage')
    root, output = Path(root).resolve(), Path(output).resolve()
    if root == output or root in output.parents or output in root.parents:
        raise ValueError('Input and output roots must be separate, non-nested directories')
    if output.exists() and any(output.iterdir()) and not resume:
        raise ValueError('Output must be empty; use --resume for a matching run')
    items = discover(root)
    if page_ids:
        items = [i for i in items if i['page_id'] in page_ids]
    if limit:
        pages = sorted({i['page_id'] for i in items})[:limit]
        items = [i for i in items if i['page_id'] in pages]
    if not items:
        raise ValueError('No selected observations')
    run_config = {'rule_version': VERSION, 'input': str(root), 'config': config,
                  'config_fingerprint': fingerprint(config), 'observation_ids': [i['observation_id'] for i in items]}
    run_path = output/'run.json'
    if resume and run_path.exists() and json.loads(run_path.read_text()) != run_config:
        raise ValueError('Resume inputs/config/version do not match; select a new output')
    write_json(run_path, run_config)
    scope_rejections = {page: {'basis': 'confirmed_special_sample'} for page in config['quality']['confirmed_dense_pages']}
    # A page-level pass precedes every viewport cleanup, so another viewport cannot bypass scope exclusion.
    for item in items:
        try:
            source = json.loads(Path(item['hierarchy']).read_text())
            evidence = scope_evidence(source, config['quality'])
            if evidence:
                scope_rejections[item['page_id']] = {'basis': 'joint_dense_table_rule', 'observation_id': item['observation_id'], **evidence}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # The isolated processing job will publish the malformed observation as review.
    write_json(output/'scope-exclusions.json', scope_rejections)
    gate = threading.BoundedSemaphore(config['pixel_workers'])
    counts = Counter()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=config['workers']) as pool:
        futures = [pool.submit(process_one, item, output, config, scope_rejections, gate, resume, stop_after) for item in items]
        for done, future in enumerate(as_completed(futures), 1):
            record = future.result()
            counts[record['status']] += 1
            counts['resumed'] += record['resumed']
            if done == 1 or done % 50 == 0 or done == len(items):
                print(json.dumps({'completed': done, 'total': len(items), 'statuses': dict(counts),
                                  'seconds': round(time.monotonic()-started, 1)}, ensure_ascii=False), flush=True)
    def results():
        for item in items:
            yield json.loads((output/'observations'/item['observation_id']/'result.json').read_text())
    write_jsonl(output/'quality'/'index.jsonl', (r['quality'] for r in results()))
    write_jsonl(output/'crops'/'manifest.jsonl', (c for r in results() if r['quality']['status'] == 'accepted' for c in r['crops']))
    write_jsonl(output/'crops'/'trainable.jsonl', (c for r in results() if stop_after == 'final-filter' and r['quality']['status'] == 'accepted' for c in r['crops'] if c['status'] == 'accepted'))
    write_jsonl(output/'split_labels'/'manifest.jsonl', (r['split_label'] for r in results() if r['split_label'] is not None))
    write_jsonl(output/'split_labels'/'trainable.jsonl', (r['split_label'] for r in results()
                                                       if r['split_label'] and r['split_label']['supervision_eligible']))
    summary = {'observations': len(items), 'pages': len({i['page_id'] for i in items}), 'completed_stage': stop_after,
               'statuses': dict(counts), 'seconds': round(time.monotonic()-started, 2)}
    write_json(output/'reports'/'run-summary.json', summary)
    return summary
