"""DCGen-inspired pixel separator fallback, kept separate from DOM/label geometry.

Reference: https://github.com/WebPAI/DCGen/blob/main/utils.py
ImgSegmentation.cut_img_bbox uses low-variance windows and uniform-row brightness jumps.
This is an independent, vectorized adaptation, with protected-box gates and explicit VLM approval.
It does not import upstream's browser/LLM code or reproduce its full UI-code pipeline.
"""
from copy import deepcopy
import math
from rules import union, validate, apply_operations
from frontier import large_region_requirements
from semantic_cleanup import generic_wrapper


def separation_lines(gray, policy=None):
    """Find soft blank bands and hard brightness changes along rows; coordinates are crop pixels."""
    import numpy as np
    policy = policy or {}
    a = np.asarray(gray, dtype=np.float32)
    if a.ndim != 2 or min(a.shape) < 2:
        return []
    variance = policy.get('pixel_variance_threshold', 50)
    difference = policy.get('pixel_difference_threshold', 45)
    portion = policy.get('pixel_difference_portion', .9)
    window = max(2, int(policy.get('pixel_window_px', 24)))
    means, squares = a.mean(axis=1, dtype=np.float64), (a*a).mean(axis=1, dtype=np.float64)
    row_var = np.maximum(0, squares-means*means)
    m = np.r_[0, np.cumsum(means)]; q = np.r_[0, np.cumsum(squares)]
    def block_var(lo, hi):
        return max(0, (q[hi]-q[lo])/(hi-lo)-((m[hi]-m[lo])/(hi-lo))**2)
    lines = []
    for end in range(2*window, len(a)-window):
        if block_var(end-window, end) < variance and (
            block_var(end-2*window, end-window) > variance or block_var(end, end+window) > variance):
            lines.append((end-window/2, 'soft_blank_band'))
    previous = None
    for i in np.flatnonzero(row_var < variance):
        if previous is not None and np.mean(np.abs(a[i]-a[previous]) > difference) > portion:
            lines.append((float(i), 'hard_background_change'))
        previous = i
    return sorted(set(lines))


def sharpen_gray(image, policy=None):
    """Prepare one sharpened grayscale image; RGB buffers are unnecessary for DCGen."""
    from PIL import ImageEnhance
    policy = policy or {}
    gray = image if image.mode == 'L' else image.convert('L')
    return ImageEnhance.Sharpness(gray).enhance(policy.get('pixel_sharpness', 6))


def pixel_partition(image, bbox, protected=(), policy=None, prepared=False):
    """Recursively propose bounded cut zones without intersecting protected component boxes.

    Zones may include whitespace and are NOT training labels. Returned cuts are global CSS px.
    ``prepared`` skips per-call RGB/grayscale conversion when the caller already supplied
    one sharpened grayscale image for all oversized roots on the page.
    """
    import numpy as np
    policy = policy or {}
    x, y, w, h = bbox
    bounds = [max(0, math.floor(x)), max(0, math.floor(y)), min(image.width, math.ceil(x+w)), min(image.height, math.ceil(y+h))]
    image = image if prepared else sharpen_gray(image, policy)
    cuts, rejected = [], []
    max_depth = policy.get('pixel_max_depth', 2)
    min_size = policy.get('pixel_min_region_px', 64)
    def recurse(rect, depth):
        if depth >= max_depth:
            return [rect]
        arr = np.asarray(image.crop(rect), dtype=np.float32)
        options = []
        for axis in (1, 0):
            oriented = arr if axis == 1 else arr.T
            length = oriented.shape[0]
            for coord, method in separation_lines(oriented, policy):
                line = rect[axis]+int(round(coord))
                if line-rect[axis] < min_size or rect[axis+2]-line < min_size:
                    continue
                crossing = [b for b in protected if b[axis]+.5 < line < b[axis]+b[axis+2]-.5 and
                    b[1-axis] < rect[3-axis] and b[1-axis]+b[3-axis] > rect[1-axis]]
                if crossing:
                    rejected.append({'axis': axis, 'line': line, 'reason': 'crosses_recorded_component', 'count': len(crossing)})
                    continue
                # Prefer wide background transitions, otherwise balanced whitespace cuts.
                balance = min(line-rect[axis], rect[axis+2]-line)/max(1,length)
                options.append((balance+(method == 'hard_background_change')*.05, axis, line, method))
        if not options:
            return [rect]
        _, axis, line, method = max(options, key=lambda item: (item[0], -item[1], -item[2]))
        first, second = list(rect), list(rect); first[axis+2] = line; second[axis] = line
        cuts.append({'axis': 'y' if axis else 'x', 'coordinate': line, 'method': method})
        return recurse(first, depth+1)+recurse(second, depth+1)
    zones = recurse(bounds, 0)
    return {'zones': [[a,b,c-a,d-b] for a,b,c,d in zones], 'cuts': cuts,
        'blocked_cut_count': len(rejected), 'method': 'dcgen_variance_brightness_adaptation_v1'}


def _inside(box, zone, tolerance=.75):
    """Check complete containment in global CSS xywh, allowing measured subpixel edges."""
    return all((box[i] >= zone[i]-tolerance and box[i]+box[i+2] <= zone[i]+zone[i+2]+tolerance) for i in (0,1))


def pixel_candidates(tree, image_path, policy=None):
    """Try pixels ONLY for still-oversized non-atomic L1 after child-frontier normalization.

    Existing components are preserved; empty/no-child regions get proposed foreground bounds,
    which remain unverified until a VLM accepts the COMPLETE partition.
    """
    policy = policy or {}
    if policy.get('pixel_fallback_enabled', True) is not True:
        return [], []
    checks = [check for check in large_region_requirements(tree, policy)
              if check['status'] == 'requires_visual_decision']
    proposed, evidence = [], []
    if not checks:
        return proposed, evidence
    from PIL import Image
    import numpy as np
    index = {n['node_id']: n for n in tree['nodes']}; children = {}
    for n in index.values():children.setdefault(n['parent_id'], []).append(n)
    with Image.open(image_path) as image:
        if image.size != (tree['capture']['width'], tree['capture']['height']):
            raise ValueError('Pixel fallback image/hierarchy dimensions differ')
        gray = sharpen_gray(image, policy)
        for check in checks:
            root = index[check['node_id']]; frontier, opened = [], []
            def descend(n):
                kids = children.get(n['node_id'], [])
                if kids and (n['node_id'] == root['node_id'] or generic_wrapper(n)):
                    opened.append(n['node_id'])
                    for child in kids:descend(child)
                else:frontier.append(n)
            if children.get(root['node_id']):descend(root)
            protected = [n['bbox'] for n in frontier]
            partition = pixel_partition(gray, root['bbox'], protected, policy, prepared=True)
            audit = {'root_id': root['node_id'], **partition}
            evidence.append(audit)
            if len(partition['zones']) < 2:
                audit['status'] = 'no_safe_pixel_separator';continue
            parts=[]; assigned=set()
            for zone in partition['zones']:
                members = [n for n in frontier if _inside(n['bbox'],zone)]
                if members:
                    target = union([n['bbox'] for n in members]); assigned.update(n['node_id'] for n in members)
                    basis = 'existing_member_union'
                elif frontier:
                    continue  # Whitespace is not a detection target.
                else:
                    x,y,w,h=zone; gray=np.asarray(image.crop((x,y,x+w,y+h)).convert('L'), dtype=np.float32)
                    border=np.r_[gray[0],gray[-1],gray[:,0],gray[:,-1]]
                    ys,xs=np.nonzero(np.abs(gray-np.median(border)) > policy.get('pixel_foreground_difference',12))
                    if len(xs)<policy.get('pixel_min_foreground_pixels',64):continue
                    target=[float(x+xs.min()),float(y+ys.min()),float(xs.max()-xs.min()+1),float(ys.max()-ys.min()+1)]
                    if min(target[2:]) < policy.get('pixel_min_target_side_px',16):continue
                    basis='pixel_foreground_unverified'
                parts.append({'partition_bbox':zone,'target_bbox':target,'members':[n['node_id'] for n in members],'target_basis':basis})
            if len(parts)<2 or assigned != {n['node_id'] for n in frontier}:
                audit['status']='insufficient_target_parts_or_unassigned_components';continue
            descendants=set(opened)|assigned
            proposed.append({'candidate_id':f'p{len(proposed)+1:03d}','root_id':root['node_id'],
                'root_bbox':root['bbox'],'basis':partition['method'],'parts':parts,'opened_wrappers':opened,
                'protected_ids':sorted(descendants),'necessity':'child_frontier_rules_could_not_split_oversized_L1',
                'cuts':partition['cuts']})
            audit['status']='proposed_for_vlm_only'
    return proposed, evidence


def apply_pixel_decisions(tree, proposed, decisions):
    """Apply only model-approved fixed partitions; keep recorded atoms and one parent per node."""
    if not isinstance(decisions,list):raise ValueError('pixel_splits must be an array')
    choices={p['candidate_id']:p for p in proposed};result=deepcopy(tree);trail=[];rejected=[];seen=set()
    for decision in decisions:
        try:
            if not isinstance(decision,dict) or set(decision)-{'candidate_id','decision'}:
                raise ValueError('Pixel decision may only select a fixed candidate')
            key=decision.get('candidate_id')
            if key not in choices or key in seen:raise ValueError('Unknown/duplicate pixel candidate')
            seen.add(key)
            if decision.get('decision') not in ('accept','reject','uncertain'):raise ValueError('Invalid pixel decision')
            if decision['decision']!='accept':continue
            proposal=choices[key];idx={n['node_id']:n for n in result['nodes']};root=idx[proposal['root_id']]
            if root['parent_id'] is not None or root['bbox']!=proposal['root_bbox']:
                raise ValueError('Pixel root changed after proposal')
            updated=deepcopy(result);active={n['node_id']:n for n in updated['nodes']};ops=[]
            # Open only generic wrapper descendants specified by the rule candidate.
            for wrapper in reversed(proposal['opened_wrappers']):
                if wrapper != root['node_id']:
                    updated,changes=apply_operations(updated,[{'op':'contract','node':wrapper,'reason':'rule_open_wrapper_for_approved_pixel_partition'}]);ops.extend(changes)
            active={n['node_id']:n for n in updated['nodes']}
            for pos,part in enumerate(proposal['parts']):
                key_new=f"pixel_region_{proposal['root_id']}_{pos}"
                occupied=set(active)|set(updated.get('postprocess_sources',{}))
                while key_new in occupied:key_new+='_new'
                region=deepcopy(root); region.update(node_id=key_new,parent_id=root['node_id'],bbox=list(part['target_bbox']),layout_bbox=list(part['target_bbox']),
                    struct='block',kind='group',subtype='pixel_visual_region',semantic_hint='visual_region',
                    postprocess_generated=True,source_node_ids=part['members'] or [root['node_id']],
                    selector='',tag='',source_dom_parent_id=None,interaction_role=None,text_role=None,
                    region_basis=part['target_basis'],partition_bbox=part['partition_bbox'],
                    reasons=['vlm_approved_pixel_region_unverified'],fragments=[])
                updated['nodes'].append(region);active[key_new]=region
                for member in part['members']:
                    if member not in active or not _inside(active[member]['bbox'],part['partition_bbox']):
                        raise ValueError('Pixel member changed or crosses boundary')
                    active[member]['parent_id']=key_new
                ops.append({'op':'pixel_region','node':key_new,'bbox':region['bbox'],'members':part['members'],'reason':'vlm_approved_pixel_partition'})
            # Contract after adding children: avoid ignoring the entire old root foreground.
            updated,changes=apply_operations(updated,[{'op':'contract','node':root['node_id'],'reason':'rule_replace_approved_pixel_root'}]);ops.extend(changes)
            validate(updated)
            result=updated;trail.extend(ops)
        except (ValueError,KeyError,TypeError) as error:
            rejected.append({'decision':decision,'error':str(error)})
    return result,trail,rejected
