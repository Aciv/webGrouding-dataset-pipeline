"""Pure geometry and source-graph contracts; all internal boxes are CSS-pixel xyxy."""
import math
from copy import deepcopy

VERSION = 'component_slices_v5'


def xyxy(box):
    """Validate source CSS xywh and convert explicitly to original-image CSS xyxy."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError('invalid_bbox')
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in box):
        raise ValueError('invalid_bbox')
    x, y, w, h = box
    if w <= 0 or h <= 0:
        raise ValueError('invalid_bbox')
    return [float(x), float(y), float(x + w), float(y + h)]


def area(box):
    """Return area of a CSS xyxy box, treating nonintersection as zero."""
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def intersection(a, b):
    """Return the CSS xyxy intersection; caller checks positive area."""
    return [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]


def union(boxes):
    """Return tight CSS xyxy union of a nonempty collection of source boxes."""
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def contains(a, b, tolerance=0.01):
    """Check complete xyxy containment with explicit CSS-pixel tolerance."""
    return a[0] - tolerance <= b[0] and a[1] - tolerance <= b[1] and a[2] + tolerance >= b[2] and a[3] + tolerance >= b[3]


def graph_depths(nodes):
    """Validate unique IDs, references and acyclicity; compute depth without a depth limit."""
    index = {n['node_id']: n for n in nodes}
    if len(index) != len(nodes):
        raise ValueError('duplicate_node_id')
    depths = {}
    for key in index:
        path, seen, current = [], set(), key
        while current is not None and current not in depths:
            if current not in index:
                raise ValueError('missing_parent_reference')
            if current in seen:
                raise ValueError('cyclic_parent_reference')
            path.append(current)
            seen.add(current)
            current = index[current].get('parent_id')
        depth = depths[current] + 1 if current is not None else 0
        for member in reversed(path):
            depths[member] = depth
            depth += 1
    return depths


def normalize(source, observation_id):
    """Copy candidate graph, preserving source facts; reject obsolete v13 and malformed geometry."""
    version = source.get('rule_version', '')
    if 'clean5_v13' in version or version.startswith(('visual_layers_fusion', 'component_slices_')) or source.get('schema') == 'gui_component_graph_v1':
        raise ValueError('obsolete_postprocess_input')
    capture = source['capture']
    width, height = capture['width'], capture['height']
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 or int(v) != v for v in (width, height)):
        raise ValueError('invalid_capture_size')
    depths = graph_depths(source['nodes'])
    nodes = []
    for original in source['nodes']:
        node = deepcopy(original)
        node.update(bbox=xyxy(original['bbox']), source_bbox_xywh=deepcopy(original['bbox']),
                    source_parent_id=original.get('parent_id'), source_layer=original.get('layer'),
                    derived_depth=depths[original['node_id']],
                    global_node_id=f"{observation_id}/{original['node_id']}",
                    detection_eligible=original.get('disposition') == 'detection',
                    source_detection_eligible=original.get('disposition') == 'detection',
                    supervision_reason='source_detection' if original.get('disposition') == 'detection' else 'source_structure_only')
        node.setdefault('source_node_ids', [node['node_id']])
        # Missing pixel visibility/occlusion evidence stays unknown, regardless of ARIA facts.
        node['pixel_visibility'] = original.get('pixel_visibility', 'unknown')
        node['occlusion'] = original.get('occlusion', 'unknown')
        nodes.append(node)
    return nodes, int(width), int(height)
