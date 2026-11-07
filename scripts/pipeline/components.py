"""Unified current dataset pipeline: viewport pre-filter, component processing, post-filter and validated publication."""
from collections import Counter
import json
from pathlib import Path
from scripts.offline_postprocess.application import run_dataset
from scripts.offline_postprocess.config import load_config, fingerprint
from scripts.offline_postprocess.domain import VERSION
from scripts.offline_postprocess.infrastructure import write_json, sha256_file, discover
from scripts.offline_postprocess.statistics import compute_statistics
from scripts.offline_postprocess.slicing_statistics import compute_slicing_statistics
from scripts.offline_postprocess.validate import validate_dataset
from scripts.page_filter.report import summarize_filtering


def run_component_pipeline(input_root, workdir, config_path=None, resume=False, limit=0,
                           baseline=None, workers=None, pixel_workers=None, page_ids=None,
                           stop_after='final-filter'):
    """Run every offline stage with one YAML and publish a versioned final index only after validation.

    Stage indexes preserve page/viewport groups and source fingerprints. They reference
    original CSS-pixel xyxy crop manifests, and cannot be passed to the legacy v13 packer.
    """
    config = load_config(config_path)
    stages = {'filter': 'index-filtered.json', 'postprocess': 'index-postprocessed.json',
              'final-filter': 'index-final.json'}
    if stop_after not in stages:
        raise ValueError('Unknown component stop stage')
    if limit < 0:
        raise ValueError('limit must be nonnegative')
    for key, value in [('workers', workers), ('pixel_workers', pixel_workers)]:
        if value is not None:
            if not 1 <= value <= 64:
                raise ValueError(f'{key} must be 1..64')
            config[key] = value
    root = Path(workdir).resolve()
    # A matching completed stage can be reused only while source and output hashes agree.
    checkpoint = root/stages[stop_after]
    if resume and (root/'run.json').is_file():
        old_run = json.loads((root/'run.json').read_text())
        items = discover(input_root)
        if page_ids:
            items = [i for i in items if i['page_id'] in page_ids]
        if limit:
            selected = sorted({i['page_id'] for i in items})[:limit]
            items = [i for i in items if i['page_id'] in selected]
        expected = {'rule_version': VERSION, 'input': str(Path(input_root).resolve()), 'config': config,
                    'config_fingerprint': fingerprint(config),
                    'observation_ids': [i['observation_id'] for i in items]}
        if old_run != expected:
            raise ValueError('Resume inputs/config/version do not match; select a new output')
        saved = json.loads(checkpoint.read_text()) if checkpoint.is_file() else {}
        records = {o['observation_id']: o for o in saved.get('observations', [])}
        same = (set(records) == set(expected['observation_ids'])
                and saved.get('stage') == {'filter': 'filtered', 'postprocess': 'postprocessed', 'final-filter': 'final'}[stop_after]
                and saved.get('config_fingerprint') == fingerprint(config))
        for item in items:
            old = records.get(item['observation_id'], {})
            same = same and all(Path(item[key]).is_file() and sha256_file(item[key]) == old.get(hash_key)
                                for key, hash_key in [('hierarchy', 'source_hash'), ('image', 'image_sha256')])
            if stop_after != 'filter':
                same = same and bool(old.get('hierarchy')) and Path(old['hierarchy']).is_file() and sha256_file(old['hierarchy']) == old.get('hierarchy_sha256')
        if stop_after == 'final-filter':
            same = same and saved.get('publication_status') == 'validated'
            same = same and set(saved.get('artifacts', {})) >= {'quality', 'detection_crops', 'split_labels', 'validation'}
            same = same and all(Path(a['path']).is_file() and sha256_file(a['path']) == a['sha256']
                                for a in saved.get('artifacts', {}).values())
        if same:
            return {'completed_stage': stop_after, 'resumed': True, 'observations': len(items),
                    'stage_index': str(checkpoint), 'final_index': str(checkpoint) if stop_after == 'final-filter' else None,
                    'validation_passed': stop_after == 'final-filter'}
    # Invalidate downstream publication markers before recomputing any earlier stage.
    if resume:
        for filename in stages.values():
            (root/filename).unlink(missing_ok=True)
    summary = run_dataset(input_root, root, config, resume=resume, limit=limit, page_ids=page_ids,
                          stop_after=stop_after)
    run = json.loads((root/'run.json').read_text())
    filtered, postprocessed, final = [], [], []
    for oid in run['observation_ids']:
        folder = root/'observations'/oid
        result = json.loads((folder/'result.json').read_text())
        q = result['quality']
        base = {k: q.get(k) for k in ['observation_id', 'page_id', 'viewport', 'group_id',
                                     'source_hierarchy', 'source_image', 'source_hash', 'image_sha256',
                                     'rule_version', 'config_fingerprint', 'original_size']}
        pre = result.get('pre_filter', {'status': 'review', 'reason_codes': ['source_validation_failed']})
        filtered.append({**base, 'status': pre['status'], 'reason_codes': pre['reason_codes'],
                         'audit': str(folder/'result.json')})
        hierarchy = folder/'hierarchy.json'
        post_status = result.get('postprocess_status', 'skipped' if pre['status'] != 'accepted' else 'review')
        postprocessed.append({**base, 'status': post_status,
            'hierarchy': str(hierarchy) if hierarchy.exists() else None,
            'hierarchy_sha256': result.get('artifact_sha256'), 'candidate_crops': len(result['crops']),
            'pre_filter_status': pre['status'], 'audit': str(folder/'result.json')})
        final.append({**base, 'status': q['status'], 'reason_codes': q['reason_codes'],
                      'hierarchy': str(hierarchy) if hierarchy.exists() else None,
                      'hierarchy_sha256': result.get('artifact_sha256'),
                      'detection_eligible': q['status'] == 'accepted',
                      'published_crops': sum(c['status'] == 'accepted' for c in result['crops']) if q['status'] == 'accepted' else 0,
                      'split_supervision_eligible': bool(q['status'] == 'accepted' and result.get('split_label') and result['split_label']['supervision_eligible']),
                      'audit': str(folder/'result.json')})
    def stage_index(stage, records, parent=None):
        return {'schema': 'gui_component_dataset_index_v1', 'pipeline': 'filter_components_final_filter',
                'rule_version': VERSION, 'stage': stage, 'source_root': str(Path(input_root).resolve()),
                'config_fingerprint': fingerprint(config), 'parent': parent,
                'statuses': dict(Counter(r['status'] for r in records)), 'observations': records,
                'coordinate_space': 'original_image_css_pixels', 'bbox_format': 'xyxy'}
    write_json(root/'index-filtered.json', stage_index('filtered', filtered))
    if stop_after == 'filter':
        return {**summary, 'stage_index': str(root/'index-filtered.json')}
    parent = {'path': str(root/'index-filtered.json'), 'sha256': sha256_file(root/'index-filtered.json')}
    write_json(root/'index-postprocessed.json', stage_index('postprocessed', postprocessed, parent))
    if stop_after == 'postprocess':
        return {**summary, 'stage_index': str(root/'index-postprocessed.json')}
    compute_statistics(root, config['workers'])
    compute_slicing_statistics(root, baseline)
    summarize_filtering(root)
    validation = validate_dataset(root, config['workers'])
    if not validation['passed']:
        raise ValueError('Final index withheld: independent validation failed; inspect reports/validation.json')
    parent = {'path': str(root/'index-postprocessed.json'), 'sha256': sha256_file(root/'index-postprocessed.json')}
    index = stage_index('final', final, parent)
    index['publication_status'] = 'validated'
    index['artifacts'] = {name: {'path': str(root/path), 'sha256': sha256_file(root/path)} for name, path in [
        ('quality', 'quality/index.jsonl'), ('detection_crops', 'crops/trainable.jsonl'),
        ('split_labels', 'split_labels/trainable.jsonl'), ('validation', 'reports/validation.json')]}
    index['artifacts']['filter_report'] = {'path': str(root/'reports/filter-summary.json'),
                                          'sha256': sha256_file(root/'reports/filter-summary.json')}
    write_json(root/'index-final.json', index)
    return {**summary, 'final_index': str(root/'index-final.json'), 'validation_passed': True}
