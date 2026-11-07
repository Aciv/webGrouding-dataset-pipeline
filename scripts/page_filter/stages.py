"""Two-stage viewport filtering: raw xywh source geometry, then actual/core crop RGB evidence."""
import math
import numpy as np
from .rules import Rules, inspect_geometry


def pre_filter_source(source, policy):
    """Audit raw CSS-xywh labels before cleanup; geometry proxies do not prove CSS responsiveness."""
    if not policy['enabled']:
        return {'status': 'accepted', 'reason_codes': [], 'responsive_status': 'unknown', 'checked': False}
    rules = Rules(min_boxes=0, min_outside_boxes=policy['min_outside_boxes'],
                  max_outside_ratio=policy['max_outside_ratio'], tolerance_px=policy['tolerance_px'],
                  min_layout_overflow_ratio=policy['min_layout_overflow_ratio'],
                  layout_overflow_policy=policy['layout_overflow_policy'])
    geometry = inspect_geometry(source, rules)
    return {'status': ('accepted' if geometry['valid'] else
                       'rejected' if policy['layout_overflow_policy'] == 'reject' else 'review'),
            'reason_codes': geometry['reasons'], 'checked': True,
            'responsive_status': 'unknown', 'basis': 'source_geometry_and_layout_proxies',
            'rule_version': 'viewport_pre_filter_v3', 'rules': rules.to_json(), 'geometry': geometry}


def color_evidence(rgb, policy):
    """Measure RGB uniformity and near-empty background without using annotation counts or ARIA visibility."""
    a = np.asarray(rgb, dtype=np.uint8)
    if a.ndim != 3 or a.shape[2] != 3 or not a.size:
        raise ValueError('invalid_rgb_analysis_region')
    flat = a.reshape(-1, 3)
    ranges = flat.max(axis=0).astype(np.int16)-flat.min(axis=0).astype(np.int16)
    max_range = int(ranges.max())
    if max_range <= policy['max_color_range']:
        return {'rejected': True, 'kind': 'solid_color', 'max_channel_range': max_range,
                'foreground_ratio': 0.0, 'edge_ratio': 0.0, 'dominant_color_rgb': flat[0].tolist()}
    quantized = a.astype(np.int32)//8
    codes = quantized[..., 0]*1024+quantized[..., 1]*32+quantized[..., 2]
    mode = int(np.bincount(codes.ravel(), minlength=32768).argmax())
    background = np.array([mode//1024*8+4, (mode//32)%32*8+4, mode%32*8+4], dtype=np.int16)
    foreground = np.max(np.abs(a.astype(np.int16)-background), axis=2) > policy['background_tolerance']
    foreground_ratio = float(foreground.mean())
    a16 = a.astype(np.int16)
    edges, pairs = 0, 0
    for axis in (0, 1):
        differences = np.max(np.abs(np.diff(a16, axis=axis)), axis=2)
        edges += int(np.count_nonzero(differences > policy['edge_difference']))
        pairs += differences.size
    edge_ratio = edges/max(1, pairs)
    rejected = foreground_ratio <= policy['max_foreground_ratio'] and edge_ratio <= policy['max_edge_ratio']
    return {'rejected': bool(rejected), 'kind': 'near_empty_background' if rejected else 'content_present',
            'max_channel_range': max_range, 'foreground_ratio': foreground_ratio,
            'edge_ratio': edge_ratio, 'dominant_color_rgb': background.tolist()}


def post_filter_crops(analysis_rgb, original_width, crops, policy):
    """Check unannotated RGB for each actual/core CSS-xyxy crop; one failure rejects its whole viewport."""
    if not policy['enabled']:
        return {'checked': False, 'rejected': False, 'scope': 'observation', 'failed_crop_ids': [], 'crops': []}
    scale_x = analysis_rgb.shape[1]/original_width
    def region(box):
        left, right = math.floor(box[0]*scale_x), math.ceil(box[2]*scale_x)
        return analysis_rgb[math.floor(box[1]):math.ceil(box[3]), left:right]
    records, failed = [], []
    for crop in crops:
        actual = color_evidence(region(crop['crop_bbox']), policy)
        core = color_evidence(region(crop['core_bbox']), policy) if policy['check_core'] else None
        scopes = (['crop'] if actual['rejected'] else [])+(['core'] if core and core['rejected'] else [])
        record = {'crop_id': crop['crop_id'], 'crop_bbox': crop['crop_bbox'], 'core_bbox': crop['core_bbox'],
                  'actual': actual, 'core': core, 'rejected': bool(scopes), 'trigger_scopes': scopes}
        records.append(record)
        if scopes:
            failed.append(crop['crop_id'])
    return {'checked': True, 'rejected': bool(failed), 'scope': 'observation',
            'reason_codes': ['blank_or_solid_crop'] if failed else [],
            'failed_crop_ids': failed, 'crops': records, 'rule_version': 'viewport_post_filter_v3',
            'policy': policy, 'pixel_basis': 'raw_rgb_before_annotation_overlay'}
