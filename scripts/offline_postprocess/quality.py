"""Source and image quality evidence; no OCR, VLM or browser dependency."""
from collections import Counter
from .domain import area, xyxy, contains


def scope_evidence(source, policy):
    """Detect extreme table candidates from joint count, tiny content, table area and repetition."""
    nodes = [n for n in source.get('nodes', []) if n.get('disposition') == 'detection']
    count = len(nodes)
    if count < policy['dense_min_targets']:
        return None
    width, height = source['capture']['width'], source['capture']['height']
    boxes = [xyxy(n['bbox']) for n in nodes]
    small = sum(b[2] - b[0] <= 200 and b[3] - b[1] <= 32 for b in boxes) / max(1, count)
    tables = [b for n, b in zip(nodes, boxes) if n.get('struct') == 'table']
    table_ratio = max((area(b) / (width * height) for b in tables), default=0)
    row_counts = Counter(round((b[1] + b[3]) / 8) for b in boxes)
    repeated = sum(v for v in row_counts.values() if v >= 3) / max(1, count)
    if small >= policy['dense_small_ratio'] and table_ratio >= policy['dense_table_area_ratio'] and repeated >= 0.4:
        return {'target_count': count, 'small_target_ratio': small,
                'largest_table_area_ratio': table_ratio, 'repeated_row_ratio': repeated}
    return None


def geometry_evidence(nodes, width, height, policy):
    """Report source target overflow; layout_bbox overflow remains a warning, not a rejection."""
    bounds = [0, 0, width, height]
    invalid = [n['node_id'] for n in nodes if n['detection_eligible'] and not contains(bounds, n['bbox'], policy['geometry_tolerance_px'])]
    layout_overflows = 0
    for n in nodes:
        if n.get('layout_bbox'):
            try:
                b = xyxy(n['layout_bbox'])
                layout_overflows += int(b[0] < -2 or b[2] > width + 2)
            except ValueError:
                pass
    return {'outside_target_ids': invalid, 'layout_overflow_warnings': layout_overflows}
