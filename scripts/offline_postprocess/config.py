"""Single YAML configuration for deterministic offline processing and resource budgets."""
import hashlib
import json
import math
from copy import deepcopy

DEFAULT = {
    'workers': 12, 'pixel_workers': 4,
    'quality': {
        'confirmed_dense_pages': ['curlie-00322976-p002'],
        'suspect_observations': ['curlie-00360564-p001/desktop', 'curlie-00360564-p001/mobile', 'curlie-00360564-p001/tablet'],
        'review_unconfirmed_error_page_peers': True,
        'confirmed_error_observations': {'curlie-00525691/mobile': {'reason': 'confirmed_domain_parking', 'image_sha256': '1b9f8cc7165788e092a32835a2c77a1c285a4181fc3de422fe9a4df6db48879b', 'basis': 'historical_filter_visual_audit_2026_11_07'}, 'curlie-00319614/desktop': {'reason': 'confirmed_captcha', 'image_sha256': '01f75275446f88796e6296c6a363fbe34fd1ab3281e2be50d83039188f69ca4a', 'basis': 'visual_audit_2026_11_07'}, 'curlie-00319614/mobile': {'reason': 'confirmed_captcha', 'image_sha256': '898cb004e82303ad1025475b9d010d8424ab934fa0275c266390197bf4245863', 'basis': 'visual_audit_2026_11_07'}, 'curlie-00319614/tablet': {'reason': 'confirmed_captcha', 'image_sha256': '773f7affa562a520be71f7b60d0fd6f80e0f057f971ba64f2252bc80f443e2ab', 'basis': 'visual_audit_2026_11_07'}, 'curlie-00062587-p001/desktop': {'reason': 'confirmed_service_error', 'image_sha256': 'df8009b1ace1328133e962d939a12f053ca552f3119a6741477a237746a5e3c2', 'basis': 'visual_audit_2026_11_07'}, 'curlie-00342576-p001/tablet': {'reason': 'confirmed_application_error', 'image_sha256': '7ccfc90c37ac4f8d4b523d4465481fcca5004fb18328b451f4b1eda8b1e30831', 'basis': 'visual_audit_2026_11_07'}, 'curlie-00342576-p002/mobile': {'reason': 'confirmed_application_error', 'image_sha256': 'bb5eaf256eb360296fe4e6b88fe10307e072e9f8aedf22bdaca328822036b09e', 'basis': 'visual_audit_2026_11_07'}, 'curlie-00466396/desktop': {'reason': 'confirmed_access_denied', 'image_sha256': '44ba8cff732d937f8b9bd1453fb9f837e19ba55ca7ad137c383ba2b52a3eed28', 'basis': 'visual_audit_2026_11_07'}},
        'dense_min_targets': 1000, 'dense_small_ratio': 0.65, 'dense_table_area_ratio': 0.45,
        'geometry_tolerance_px': 2.0,
    },
    'cleanup': {'fine_text_height': 24, 'fine_link_height': 32, 'fine_width': 200,
                'tiny_side': 20, 'group_min_members': 2, 'group_max_height': 700,
                'group_gap': 24, 'wrapper_height_ratio': 0.45},
    'pre_filter': {'enabled': True, 'min_outside_boxes': 5, 'max_outside_ratio': 0.2,
                   'tolerance_px': 2.0, 'min_layout_overflow_ratio': 0.1,
                   'layout_overflow_policy': 'reject'},
    'blank_crop_filter': {'enabled': True, 'check_core': True, 'scope': 'observation',
                          'max_color_range': 4, 'background_tolerance': 12,
                          'max_foreground_ratio': 0.0001, 'max_edge_ratio': 0.0002,
                          'edge_difference': 12},
    'local_regions': {'enabled': True, 'min_height': 1000, 'min_members': 4,
                      'max_depth': 3, 'max_roots': 2, 'max_candidates_per_axis': 160,
                      'min_gap_px': 8, 'min_part_members': 2},
    'slicing': {'trigger_height_gt': 2700, 'min_height': 900, 'max_height': 1800,
                'target_height': 1350, 'max_targets': 384, 'context_px': 96,
                'candidate_step': 30, 'materialize_crops': False,
                'critical_replan_heights': [1100, 900]},
    'pixels': {'analysis_width': 256, 'rgb_analysis_width': 512, 'pixel_variance_threshold': 50,
               'pixel_difference_threshold': 45, 'pixel_difference_portion': 0.9,
               'pixel_window_px': 24, 'max_decoded_pixels': 60000000},
    'split_supervision': {'include_fallback': False, 'allow_component_boundary_loss': True,
                          'max_missing_component_targets': 16, 'max_missing_component_ratio': 0.1,
                          'max_input_height': 2048, 'max_input_width': 384},
    'statistics': {'width_buckets': [480, 900, 1200, 1600],
                   'iou_edges': [0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 1.0]},
}


def load_config(path=None):
    """Load YAML overrides, reject unknown fields and invalid resource/height budgets."""
    config = deepcopy(DEFAULT)
    if path:
        import yaml
        with open(path, encoding='utf-8') as stream:
            override = yaml.safe_load(stream) or {}
        for key, value in override.items():
            if key not in config:
                raise ValueError(f'Unknown config key: {key}')
            if isinstance(config[key], dict):
                if not isinstance(value, dict) or set(value) - set(config[key]):
                    raise ValueError(f'Unknown or malformed config section: {key}')
                config[key].update(value)
            else:
                config[key] = value
    for key in ('workers', 'pixel_workers'):
        if type(config[key]) is not int or not 1 <= config[key] <= 64:
            raise ValueError(f'{key} must be 1..64')
    s = config['slicing']
    if not 0 < s['min_height'] <= s['target_height'] <= s['max_height'] < s['trigger_height_gt']:
        raise ValueError('Invalid height constraints')
    if s['trigger_height_gt'] + 1 < 2 * s['min_height']:
        raise ValueError('Trigger must permit at least two core slices')
    if s['max_targets'] <= 0 or s['candidate_step'] <= 0 or s['context_px'] < 0:
        raise ValueError('Invalid capacity/context/candidate settings')
    supervision = config['split_supervision']
    if (type(supervision['allow_component_boundary_loss']) is not bool
            or type(supervision['max_missing_component_targets']) is not int
            or supervision['max_missing_component_targets'] < 0
            or not math.isfinite(supervision['max_missing_component_ratio'])
            or not 0 <= supervision['max_missing_component_ratio'] <= 1):
        raise ValueError('Invalid component boundary noise budget')
    local = config['local_regions']
    if (type(local['enabled']) is not bool or not 1 <= local['max_depth'] <= 6
            or not 1 <= local['max_roots'] <= 8 or local['min_members'] < 2
            or local['min_part_members'] < 2 or local['min_height'] <= 0
            or not 1 <= local['max_candidates_per_axis'] <= 2048):
        raise ValueError('Invalid local region resource/evidence budget')
    blank = config['blank_crop_filter']
    if (type(blank['enabled']) is not bool or type(blank['check_core']) is not bool
            or blank['scope'] != 'observation' or not 0 <= blank['max_color_range'] <= 16
            or not 0 <= blank['max_foreground_ratio'] <= 0.01
            or not 0 <= blank['max_edge_ratio'] <= 0.01):
        raise ValueError('Invalid blank-crop filter policy')
    if config['pre_filter']['layout_overflow_policy'] not in {'warn', 'reject', 'ignore'}:
        raise ValueError('Invalid source layout overflow policy')
    pre = config['pre_filter']
    if (type(pre['enabled']) is not bool or type(pre['min_outside_boxes']) is not int
            or pre['min_outside_boxes'] < 1
            or not math.isfinite(pre['max_outside_ratio']) or not 0 < pre['max_outside_ratio'] <= 1
            or not math.isfinite(pre['min_layout_overflow_ratio']) or not 0 < pre['min_layout_overflow_ratio'] <= 1
            or not math.isfinite(pre['tolerance_px']) or pre['tolerance_px'] < 0):
        raise ValueError('Invalid source layout overflow thresholds')
    for key in ('width_buckets', 'iou_edges'):
        values = config['statistics'][key]
        if sorted(set(values)) != values:
            raise ValueError(f'{key} must be strictly increasing')
    if config['statistics']['iou_edges'][0] != 0 or config['statistics']['iou_edges'][-1] != 1:
        raise ValueError('IoU edges must span [0,1]')
    return config


def fingerprint(config):
    """Hash all effective settings, including budgets, for cache and artifact provenance."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False).encode()).hexdigest()
