"""Explicit, provenance-preserving corrections for saved structure candidates."""

PSEUDO_ICON_MAX_SIDE_PX = 64
PSEUDO_BLOCK_MIN_WIDTH_PX = 16
PSEUDO_BLOCK_MIN_HEIGHT_PX = 12


def correct_pseudo_icon_label(node):
    """Relabel a large pseudo-content 'graphic/icon' candidate as block/visual_block.

    The saved classifier treated any leaf pseudo-element as an icon before checking
    painted block geometry, so page-sized wrappers became graphic targets. Icon-sized
    pseudo content stays graphic; thin pseudo lines stay unchanged rather than being
    invented as blocks. The original label is retained under label_correction.
    Coordinates must already be CSS xyxy; returns True only when a label changed.
    """
    if node.get('struct') != 'graphic' or node.get('subtype') != 'icon':
        return False
    reasons = node.get('structure_reasons') or node.get('reasons') or []
    if 'pseudo_content' not in reasons:
        return False
    box = node.get('bbox') or [0, 0, 0, 0]
    width, height = box[2]-box[0], box[3]-box[1]
    if width <= PSEUDO_ICON_MAX_SIDE_PX and height <= PSEUDO_ICON_MAX_SIDE_PX:
        return False
    if width < PSEUDO_BLOCK_MIN_WIDTH_PX or height < PSEUDO_BLOCK_MIN_HEIGHT_PX:
        return False
    node['label_correction'] = {
        'rule': 'large_pseudo_content_block_v1',
        'from': {'struct':'graphic', 'subtype':'icon', 'kind':node.get('kind')},
        'reason': 'pseudo_content_is_not_icon_sized',
    }
    node['struct'] = 'block'
    node['subtype'] = 'visual_block'
    if node.get('semantic_hint') == 'icon':
        node['semantic_hint'] = 'visual_block'
    for key in ('structure_reasons', 'reasons'):
        values = node.get(key)
        if isinstance(values, list) and 'large_pseudo_content_block' not in values:
            values.append('large_pseudo_content_block')
    return True
