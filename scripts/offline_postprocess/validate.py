"""Independent artifact validation against immutable source files and global coordinate contracts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
from .domain import graph_depths, contains
from .cleanup import critical_target
from .infrastructure import read_jsonl, sha256_file, write_json


def validate_dataset(root, workers=12):
    """Verify hashes, graph references, crop transforms and height/capacity contracts; write an evidence report."""
    root = Path(root).resolve()
    run = json.loads((root/'run.json').read_text())
    config = run['config']
    policy = config['slicing']
    qualities = list(read_jsonl(root/'quality'/'index.jsonl'))
    def check(q):
        errors = []
        observation_id = q['observation_id']
        folder = root/'observations'/observation_id
        result = json.loads((folder/'result.json').read_text())
        for key, path in [('source_hash', q['source_hierarchy']), ('image_sha256', q['source_image'])]:
            if q.get(key) != sha256_file(path):
                errors.append(f'{key}_mismatch')
        if result.get('artifact_sha256'):
            if result['artifact_sha256'] != sha256_file(folder/'hierarchy.json'):
                errors.append('derived_hash_mismatch')
            graph = json.loads((folder/'hierarchy.json').read_text())
            graph_depths(graph['nodes'])
            index = {n['global_node_id']: n for n in graph['nodes']}
            for node in graph['nodes']:
                if node.get('represented_by') and not index[node['represented_by']]['detection_eligible']:
                    errors.append('inactive_representative')
                if node['detection_eligible'] and node.get('subtype') == 'pixel_content_region' and node['bbox'][3]-node['bbox'][1] > policy['max_height']:
                    errors.append('local_region_exceeds_height_budget')
            crops = result['crops']
            label = result.get('split_label')
            if label:
                h, w = q['original_size']['height'], q['original_size']['width']
                cuts = label['cut_lines_original_px']
                if cuts != sorted(set(cuts)) or any(y <= 0 or y >= h for y in cuts):
                    errors.append('invalid_cut_order')
                if (h <= policy['trigger_height_gt']) != (not cuts):
                    errors.append('trigger_mismatch')
                if label['cut_lines_normalized'] != [y/h for y in cuts]:
                    errors.append('normalized_coordinate_mismatch')
                full = {n['global_node_id'] for crop in crops if crop['status'] == 'accepted' for n in crop['targets']}
                missing = [n for n in graph['nodes'] if n['detection_eligible'] and n['global_node_id'] not in full]
                if label['supervision_eligible'] and 'allow_component_boundary_loss' in config['split_supervision']:
                    supervision = config['split_supervision']
                    if any(critical_target(n) for n in missing):
                        errors.append('eligible_label_has_critical_loss')
                    if missing and (not supervision['allow_component_boundary_loss']
                            or len(missing) > supervision['max_missing_component_targets']
                            or len(missing)/max(1, len(graph['detection_target_ids'])) > supervision['max_missing_component_ratio']):
                        errors.append('eligible_label_exceeds_component_loss_budget')
                    if label['fallback'] and not supervision['include_fallback']:
                        errors.append('fallback_label_wrongly_published')
                if crops and (crops[0]['core_bbox'][1] != 0 or crops[-1]['core_bbox'][3] != h):
                    errors.append('incomplete_page_coverage')
                for left, right in zip(crops, crops[1:]):
                    if left['core_bbox'][3] != right['core_bbox'][1]:
                        errors.append('core_gap_or_overlap')
                for crop in crops:
                    box = crop['crop_bbox']
                    if not contains(box, crop['core_bbox']):
                        errors.append('core_outside_crop')
                    if cuts and not policy['min_height'] <= box[3]-box[1] <= policy['max_height']:
                        errors.append('invalid_crop_height')
                    if cuts and not policy['min_height'] <= crop['core_bbox'][3]-crop['core_bbox'][1] <= policy['max_height']:
                        errors.append('invalid_core_height')
                    if crop['status'] == 'accepted' and len(crop['targets']) > policy['max_targets']:
                        errors.append('capacity_overflow')
                    for target in crop['targets']:
                        node = index[target['global_node_id']]
                        expected = [node['bbox'][0], node['bbox'][1]-box[1], node['bbox'][2], node['bbox'][3]-box[1]]
                        if any(abs(a-b) > 1e-8 for a, b in zip(expected, target['bbox'])):
                            errors.append('local_global_transform_mismatch')
                        if not node['detection_eligible'] or not contains(box, node['bbox']):
                            errors.append('invalid_positive')
                        parent = node.get('parent_id')
                        expected_parent = f'{observation_id}/{parent}' if parent else None
                        if target['global_parent_id'] != expected_parent:
                            errors.append('parent_reference_changed')
                    for ignore in crop['ignore_regions']:
                        if not contains([0, 0, w, box[3]-box[1]], ignore['bbox']):
                            errors.append('ignore_outside_crop')
            if q['observation_id'] in config['quality'].get('suspect_observations', []) and q['status'] == 'accepted':
                errors.append('known_alignment_suspect_published')
            confirmed = config['quality'].get('confirmed_error_observations', {}).get(q['observation_id'])
            if confirmed and q.get('image_sha256') == confirmed['image_sha256'] and q['status'] != 'rejected':
                errors.append('confirmed_error_not_rejected')
            if config.get('blank_crop_filter', {}).get('enabled'):
                post = q.get('evidence', {}).get('post_filter', {})
                if q['status'] == 'accepted' and (not post.get('checked') or post.get('rejected')
                        or len(post.get('crops', [])) != len(result['crops'])
                        or any(c.get('rejected') for c in post.get('crops', []))):
                    errors.append('accepted_observation_missing_clean_post_filter')
                if post.get('rejected') and (q['status'] != 'rejected'
                        or any(c['status'] == 'accepted' for c in result['crops'])
                        or result.get('split_label') and result['split_label']['supervision_eligible']):
                    errors.append('blank_crop_observation_leaked_to_supervision')
            # A review observation may retain failed crops/labels in its audit bundle;
            # only the published manifests below determine training membership.
        return {'observation_id': observation_id, 'errors': sorted(set(errors))}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(check, qualities))
    failures = [r for r in results if r['errors']]
    published_crops = list(read_jsonl(root/'crops'/'trainable.jsonl'))
    crop_ids = [c['crop_id'] for c in published_crops]
    if len(set(crop_ids)) != len(crop_ids):
        failures.append({'errors': ['duplicate_published_crop_id']})
    accepted_ids = {q['observation_id'] for q in qualities if q['status'] == 'accepted'}
    if any(c['observation_id'] not in accepted_ids for c in published_crops):
        failures.append({'errors': ['nonaccepted_crop_in_training_manifest']})
    labels = list(read_jsonl(root/'split_labels'/'trainable.jsonl'))
    if any(not label['supervision_eligible'] or label['observation_id'] not in accepted_ids for label in labels):
        failures.append({'errors': ['ineligible_split_label_published']})
    report = {'observations_checked': len(results), 'source_hierarchy_hashes_checked': len(results),
              'source_image_hashes_checked': len(results), 'trainable_crops_checked': len(published_crops),
              'trainable_split_labels_checked': len(labels), 'failures': failures, 'passed': not failures}
    write_json(root/'reports'/'validation.json', report)
    return report
