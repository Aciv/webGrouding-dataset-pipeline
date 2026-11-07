"""Local DCGen-style separator proposals constrained by existing source contents, before page slicing."""
import math
import numpy as np
from .domain import area, contains, union
from .pixels import separation_lines


def propose_regions(nodes, width, height, gray, policy, pixels_policy, max_height):
    """Propose bounded xyxy subregions from local pixels plus source boxes, never pixel-only object GT.

    Horizontal and vertical analysis is local to a source container. Existing atomic
    targets are protected. Returned partitions are evidence for cleanup, not page cut labels.
    """
    if not policy['enabled']:
        return []
    scale_x = gray.shape[1]/width
    index = {n['node_id']: n for n in nodes}
    roots = [n for n in nodes if n['detection_eligible']
             and (n.get('struct') in {'block', 'region', 'table', 'list'} or n.get('subtype') == 'background_image')
             and n['bbox'][3]-n['bbox'][1] >= policy['min_height']]
    roots.sort(key=lambda n: (-area(n['bbox']), n['node_id']))
    chosen, proposals = [], []
    for root in roots:
        if len(chosen) >= policy['max_roots']:
            break
        if any(contains(r['bbox'], root['bbox']) for r in chosen):
            continue
        ancestors, parent = set(), root.get('parent_id')
        while parent is not None:
            ancestors.add(parent)
            parent = index[parent].get('parent_id')
        members = [n for n in nodes if n['node_id'] != root['node_id'] and n['node_id'] not in ancestors
                   and n['detection_eligible'] and contains(root['bbox'], n['bbox'])
                   and area(n['bbox']) < area(root['bbox'])*.85
                   and (n.get('struct') not in {'block', 'region', 'table', 'list'}
                        or n['bbox'][3]-n['bbox'][1] <= 700)]
        if len(members) < policy['min_members']:
            continue
        chosen.append(root)
        cuts = []
        def partition(rect, contents, depth):
            if depth >= policy['max_depth'] or rect[3]-rect[1] <= policy['min_height']:
                return [rect]
            y0, y1 = max(0, math.floor(rect[1])), min(height, math.ceil(rect[3]))
            x0, x1 = max(0, math.floor(rect[0]*scale_x)), min(gray.shape[1], math.ceil(rect[2]*scale_x))
            local = gray[y0:y1, x0:x1]
            options = []
            for axis in (1, 0):
                oriented = local if axis == 1 else local.T
                pixel_policy = dict(pixels_policy)
                if axis == 0:
                    pixel_policy['pixel_window_px'] = max(2, round(pixel_policy['pixel_window_px']*scale_x))
                candidates = []
                for coordinate, basis in separation_lines(oriented, pixel_policy):
                    y = y0+coordinate if axis == 1 else (x0+coordinate)/scale_x
                    candidates.append((y, basis))
                ordered = sorted(contents, key=lambda n: n['bbox'][axis])
                edge = ordered[0]['bbox'][axis+2]
                for member in ordered[1:]:
                    start = member['bbox'][axis]
                    if start-edge >= policy['min_gap_px']:
                        candidates.append(((start+edge)/2, 'source_projection_gap'))
                    edge = max(edge, member['bbox'][axis+2])
                # Bound work deterministically for long uniform bands.
                candidates = sorted(set(candidates))
                stride = max(1, math.ceil(len(candidates)/policy['max_candidates_per_axis']))
                for line, basis in candidates[::stride]:
                    length = rect[axis+2]-rect[axis]
                    balance = min(line-rect[axis], rect[axis+2]-line)/max(1, length)
                    if balance < .15:
                        continue
                    if any(n['bbox'][axis]+.01 < line < n['bbox'][axis+2]-.01 for n in contents):
                        continue
                    left = [n for n in contents if n['bbox'][axis+2] <= line+.01]
                    right = [n for n in contents if n['bbox'][axis] >= line-.01]
                    if min(len(left), len(right)) < policy['min_part_members']:
                        continue
                    # Source-gap candidates also require local uniform-pixel support.
                    offset = round(line-y0) if axis == 1 else round(line*scale_x-x0)
                    band = oriented[max(0, offset-2):min(len(oriented), offset+3)]
                    if not band.size:
                        continue
                    variance = float(np.var(band.astype(np.float32)))
                    if basis == 'source_projection_gap' and variance > pixels_policy['pixel_variance_threshold']*2:
                        continue
                    score = balance + (axis == 1)*.05 + (basis != 'source_projection_gap')*.02
                    options.append((score, axis, line, basis, left, right, variance))
            if not options:
                return [rect]
            _, axis, line, basis, left, right, variance = max(options, key=lambda x: x[:3])
            a, b = list(rect), list(rect)
            a[axis+2], b[axis] = line, line
            cuts.append({'axis': 'y' if axis == 1 else 'x', 'coordinate': line, 'basis': basis,
                         'local_band_variance': variance, 'depth': depth})
            return partition(a, left, depth+1)+partition(b, right, depth+1)
        zones = partition(list(root['bbox']), members, 0)
        if len(zones) < 2:
            continue
        proposals.append({'root_id': root['node_id'], 'zones': zones, 'cuts': cuts,
                          'source_member_ids': [n['node_id'] for n in members],
                          'max_target_height': max_height, 'coordinate_space': 'original_css_xyxy',
                          'basis': 'local_dcgen_and_source_contents'})
    return proposals
