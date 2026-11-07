"""Global horizontal cut planning and immutable crop views in original CSS xyxy."""
import bisect
import math
import numpy as np
from .domain import area, contains, intersection
from .cleanup import critical_target


def plan_cuts(width, height, targets, separators, policy):
    """Plan full-width core partitions using DP, source geometry and pixel separator evidence.

    This is an offline label generator, not the inference decoder: target boxes must
    never be supplied to a model's inference path.
    """
    if height <= policy['trigger_height_gt']:
        return [], {'method': 'whole_image', 'fallback': False, 'cut_object_count': 0, 'candidates': 0}
    evidence = {0: 'endpoint', height: 'endpoint'}
    for y in range(policy['candidate_step'], height, policy['candidate_step']):
        evidence[y] = 'height_grid_fallback'
    for y, kind in separators:
        if 0 < y < height:
            evidence[int(y)] = kind
    boxes = np.array([n['bbox'] for n in targets], dtype=np.float64).reshape(-1, 4)
    # Edges are candidate boundaries; a small original-pixel margin avoids rasterized text edges.
    for box in boxes:
        for y in (math.floor(box[1])-1, math.ceil(box[3])+1):
            if 0 < y < height:
                evidence.setdefault(y, 'source_box_gap')
    # Evenly spaced feasible boundaries guarantee a connected height-constrained graph.
    count = math.ceil(height / policy['target_height'])
    count = max(math.ceil(height/policy['max_height']), min(count, math.floor(height/policy['min_height'])))
    for i in range(1, count):
        evidence.setdefault(round(height*i/count), 'height_grid_fallback')
    coordinates = sorted(evidence)
    centers = sorted((b[1]+b[3])/2 for b in boxes)
    # Only genuinely empty projection gaps receive a source-box evidence reward.
    crossing, costs = {}, {}
    weights = np.array([8.0 if critical_target(n) else 1.0 for n in targets])
    for y in coordinates:
        cross = int(np.count_nonzero((boxes[:, 1]+.01 < y) & (boxes[:, 3]-.01 > y)))
        crossing[y] = cross
        kind = evidence[y]
        reward = 2 if kind in {'soft_blank_band', 'hard_background_change'} else 1 if kind == 'source_box_gap' and cross == 0 else 0
        mask = (boxes[:, 1]+.01 < y) & (boxes[:, 3]-.01 > y)
        costs[y] = float(weights[mask].sum())*10 + (2-reward)*.3
    best = {0: (0., None)}
    min_h, max_h = policy['min_height'], policy['max_height']
    for y in coordinates[1:]:
        lo, hi = bisect.bisect_left(coordinates, y-max_h), bisect.bisect_right(coordinates, y-min_h)
        choice = None
        for previous in coordinates[lo:hi]:
            if previous not in best:
                continue
            target_count = bisect.bisect_left(centers, y) - bisect.bisect_left(centers, previous)
            if target_count > policy['max_targets']:
                continue
            cost = best[previous][0] + .4 + 8*((y-previous-policy['target_height'])/policy['target_height'])**2
            if y != height:
                cost += costs[y]
            candidate = (cost, previous)
            if choice is None or candidate < choice:
                choice = candidate
        if choice is not None:
            best[y] = choice
    if height not in best:
        raise ValueError('dense_capacity_conflict')
    cuts, current = [], height
    while best[current][1] != 0:
        current = best[current][1]
        cuts.append(current)
    cuts.reverse()
    selected = [{'y': y, 'basis': evidence[y], 'crossing_targets': crossing[y]} for y in cuts]
    return cuts, {'method': 'dcgen_and_boxes_dp', 'candidates': len(coordinates), 'selected': selected,
                  'fallback': any(evidence[y] == 'height_grid_fallback' for y in cuts),
                  'cut_object_count': sum(crossing[y] for y in cuts),
                  'max_capacity': policy['max_targets'], 'coordinate_space': 'original_css_pixels'}


def crop_views(observation_id, width, height, targets, cuts, policy, source_ignores=()):
    """Create local crop views without editing global boxes, semantics or parent relationships.

    Fragments are ignore regions, never new complete-object positives. Full targets may
    repeat across context crops; owner_core_index is a deterministic global reference.
    """
    bounds = [0, *cuts, height]
    crops = []
    global_bounds = [0, 0, width, height]
    seen_full = set()
    for pos, (top, bottom) in enumerate(zip(bounds, bounds[1:])):
        crop_top, crop_bottom = top, bottom
        if cuts:
            # Context first tries to make intersecting objects whole, with capacity and height gates.
            intersecting = [n for n in targets if area(intersection(n['bbox'], [0, top, width, bottom])) > 0]
            for node in sorted(intersecting, key=lambda n: (not critical_target(n), n['bbox'][3]-n['bbox'][1], n['node_id'])):
                new_top = max(0, min(crop_top, math.floor(node['bbox'][1])))
                new_bottom = min(height, max(crop_bottom, math.ceil(node['bbox'][3])))
                count = sum(contains([0, new_top, width, new_bottom], n['bbox']) for n in targets)
                if new_bottom-new_top <= policy['max_height'] and count <= policy['max_targets']:
                    crop_top, crop_bottom = new_top, new_bottom
            room = policy['max_height'] - (crop_bottom-crop_top)
            desired_top = max(0, crop_top-min(policy['context_px'], room//2))
            desired_bottom = min(height, crop_bottom+min(policy['context_px'], room-(crop_top-desired_top)))
            if sum(contains([0, desired_top, width, desired_bottom], n['bbox']) for n in targets) <= policy['max_targets']:
                crop_top, crop_bottom = desired_top, desired_bottom
        crop_box = [0, crop_top, width, crop_bottom]
        positives, ignores = [], []
        for node in targets:
            box = node['bbox']
            clipped = intersection(box, crop_box)
            if area(clipped) <= 0:
                continue
            local = [clipped[0], clipped[1]-crop_top, clipped[2], clipped[3]-crop_top]
            full = contains(crop_box, box) and contains(global_bounds, box)
            reference = {'global_node_id': node['global_node_id'], 'bbox': local,
                         'global_parent_id': f"{observation_id}/{node['parent_id']}" if node.get('parent_id') else None,
                         'struct': node.get('struct'), 'subtype': node.get('subtype'),
                         'semantic_role': node.get('semantic_role'), 'pixel_visibility': node.get('pixel_visibility', 'unknown'),
                         'occlusion': node.get('occlusion', 'unknown'), 'geometric_visibility': 'complete' if full else 'partial',
                         'owner_core_index': min(len(bounds)-2, max(0, bisect.bisect_right(bounds, (box[1]+box[3])/2)-1))}
            if full:
                reference.update(duplicate_context_occurrence=node['global_node_id'] in seen_full, detection_eligible=True)
                positives.append(reference)
                seen_full.add(node['global_node_id'])
            else:
                reference.update(reason='boundary_fragment', detection_eligible=False)
                ignores.append(reference)
        for source_ignore in source_ignores:
            clipped = intersection(source_ignore['bbox'], crop_box)
            if area(clipped) > 0:
                ignores.append({**source_ignore, 'bbox': [clipped[0], clipped[1]-crop_top, clipped[2], clipped[3]-crop_top]})
        reasons = []
        if len(positives) > policy['max_targets']:
            reasons.append('dense_capacity_conflict')
        if not positives:
            reasons.append('no_detection_targets')
        crops.append({'schema': 'gui_crop_view_v1', 'crop_id': f'{observation_id}/{pos:04d}',
                      'observation_id': observation_id, 'bbox_format': 'xyxy',
                      'core_bbox': [0, top, width, bottom], 'crop_bbox': crop_box,
                      'local_to_global': {'scale': [1, 1], 'translation': [0, crop_top]},
                      'targets': positives, 'ignore_regions': ignores,
                      'status': 'accepted' if not reasons else 'review', 'reason_codes': reasons})
    missing = sorted({n['global_node_id'] for n in targets} - seen_full)
    return crops, missing


def supervision_qualification(targets, crops, missing, partition, policy):
    """Grade cut pseudo-labels by actual full-target coverage, allowing bounded component-only loss."""
    index = {n['global_node_id']: n for n in targets}
    critical_missing = [key for key in missing if critical_target(index[key])]
    component_missing = [key for key in missing if key not in critical_missing]
    reasons = []
    if critical_missing:
        reasons.append('critical_target_without_complete_view')
    ratio = len(component_missing)/max(1, len(targets))
    tolerated = (policy['allow_component_boundary_loss']
                 and len(component_missing) <= policy['max_missing_component_targets']
                 and ratio <= policy['max_missing_component_ratio'])
    if component_missing and not tolerated:
        reasons.append('component_boundary_loss_over_budget')
    # Crops containing no targets can be omitted from detection, but their core
    # partitions remain valid full-page split supervision if all geometry is legal.
    if any(any(r != 'no_detection_targets' for r in c['reason_codes']) for c in crops):
        reasons.append('invalid_crop_constraints')
    if partition['fallback'] and not policy['include_fallback']:
        reasons.append('height_fallback_not_in_primary_supervision')
    return {'eligible': not reasons, 'reason_codes': reasons,
            'critical_missing_ids': critical_missing, 'component_missing_ids': component_missing,
            'component_missing_ratio': ratio, 'component_boundary_loss_tolerated': bool(component_missing and tolerated),
            'quality_tier': 'tolerated_component_boundary' if component_missing and tolerated and not critical_missing else 'complete' if not missing else 'review'}


def plan_views(observation_id, width, height, targets, separators, slicing, supervision, source_ignores=()):
    """Plan cuts/context with bounded retries for critical content; component loss alone adds no crops."""
    cuts, partition = plan_cuts(width, height, targets, separators, slicing)
    crops, missing = crop_views(observation_id, width, height, targets, cuts, slicing, source_ignores)
    qualification = supervision_qualification(targets, crops, missing, partition, supervision)
    attempts = [{'target_height': slicing['target_height'], 'critical_missing': len(qualification['critical_missing_ids']),
                 'component_missing': len(qualification['component_missing_ids']), 'crop_count': len(crops)}]
    best = (cuts, partition, crops, missing, qualification)
    if height > slicing['trigger_height_gt'] and qualification['critical_missing_ids']:
        for target_height in slicing.get('critical_replan_heights', []):
            if not slicing['min_height'] <= target_height <= slicing['max_height']:
                continue
            alternative = {**slicing, 'target_height': target_height}
            try:
                c, p = plan_cuts(width, height, targets, separators, alternative)
                views, lost = crop_views(observation_id, width, height, targets, c, alternative, source_ignores)
            except ValueError:
                continue
            grade = supervision_qualification(targets, views, lost, p, supervision)
            attempts.append({'target_height': target_height, 'critical_missing': len(grade['critical_missing_ids']),
                             'component_missing': len(grade['component_missing_ids']), 'crop_count': len(views)})
            if len(grade['critical_missing_ids']) < len(best[4]['critical_missing_ids']):
                best = (c, p, views, lost, grade)
            if best[4]['critical_missing_ids']:
                # Core-crossing costs alone may prefer too few crops even though
                # an extra overlapping view would preserve a foreground object.
                c, p = balanced_recovery_cuts(height, targets, separators, alternative)
                views, lost = crop_views(observation_id, width, height, targets, c, alternative, source_ignores)
                grade = supervision_qualification(targets, views, lost, p, supervision)
                attempts.append({'target_height': target_height, 'method': 'balanced_recovery',
                                 'critical_missing': len(grade['critical_missing_ids']),
                                 'component_missing': len(grade['component_missing_ids']), 'crop_count': len(views)})
                if len(grade['critical_missing_ids']) < len(best[4]['critical_missing_ids']):
                    best = (c, p, views, lost, grade)
            if not best[4]['critical_missing_ids']:
                break
    best[1]['critical_replan_attempts'] = attempts
    return best


def balanced_recovery_cuts(height, targets, separators, policy):
    """Try a bounded larger core count, snapping near balanced y coordinates to pixel/source evidence."""
    count = max(math.ceil(height/policy['max_height']),
                min(math.ceil(height/policy['target_height']), math.floor(height/policy['min_height'])))
    evidence = {int(y): basis for y, basis in separators if 0 < y < height}
    for node in targets:
        for y in (math.floor(node['bbox'][1])-1, math.ceil(node['bbox'][3])+1):
            if 0 < y < height:
                evidence.setdefault(y, 'source_box_gap')
    cuts, selected = [], []
    previous = 0
    for pos in range(1, count):
        remaining = count-pos
        lo = max(previous+policy['min_height'], height-remaining*policy['max_height'])
        hi = min(previous+policy['max_height'], height-remaining*policy['min_height'])
        preferred = max(lo, min(hi, round(height*pos/count)))
        candidates = {y for y in evidence if lo <= y <= hi and abs(y-preferred) <= 120} | {preferred}
        def cost(y):
            crossing = [n for n in targets if n['bbox'][1]+.01 < y < n['bbox'][3]-.01]
            return (sum(8 if critical_target(n) else 1 for n in crossing), y not in evidence, abs(y-preferred), y)
        y = min(candidates, key=cost)
        cuts.append(y)
        selected.append({'y': y, 'basis': evidence.get(y, 'height_grid_fallback'),
                         'crossing_targets': sum(n['bbox'][1]+.01 < y < n['bbox'][3]-.01 for n in targets)})
        previous = y
    return cuts, {'method': 'bounded_balanced_critical_retry', 'selected': selected,
                  'fallback': any(p['basis'] == 'height_grid_fallback' for p in selected),
                  'cut_object_count': sum(p['crossing_targets'] for p in selected), 'candidates': len(evidence),
                  'coordinate_space': 'original_css_pixels'}
