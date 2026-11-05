"""Strict original-L5+ low-detail target selection; no live DOM or visibility inference."""
from collections import Counter
from rules import validate


def classify_deep_noise(tree, policy, is_generic):
    """Classify original depths before contraction; low-detail wrappers inherit all-noise children.

    This is a dataset granularity policy, not proof that an element is invisible or useless.
    Heading, primary media, input controls and large content remain protected. No new boxes.
    """
    depths=validate(tree);index={n['node_id']:n for n in tree['nodes']};children={}
    for n in tree['nodes']:children.setdefault(n['parent_id'],[]).append(n)
    reasons={};evidence={}
    for n in sorted(tree['nodes'],key=lambda n:(depths[n['node_id']],n['node_id'])):
        key=n['node_id'];level=depths[key]+1
        if level<5:continue
        w,h=n['bbox'][2:];area=w*h;struct=n.get('struct');sub=n.get('subtype','');role=n.get('text_role')
        reason=None
        # Only leaves are scored by appearance size. Container removal requires every
        # descendant branch to have independently satisfied the low-detail policy.
        if not children.get(key):
            small_text=area<=policy.get('deep_text_max_area_px2',4096) and h<=policy.get('deep_text_max_height_px',32) and w<=policy.get('deep_text_max_width_px',256)
            small_link=area<=policy.get('deep_link_max_area_px2',6144) and h<=32 and w<=256
            if struct=='text' and sub!='heading' and role!='heading' and small_text:
                reason='deep_small_text_detail'
            elif struct=='control' and sub=='link' and small_link:
                reason='deep_small_link_detail'
            elif struct=='control' and sub=='button' and area<=1024 and max(w,h)<=32:
                reason='deep_small_button_detail'
            elif struct=='image' and area<=policy.get('deep_image_max_area_px2',2048) and max(w,h)<=policy.get('deep_image_max_side_px',64):
                reason='deep_small_image_detail'
            elif struct=='graphic' and area<=1024 and max(w,h)<=48:
                reason='deep_small_graphic_detail'
            elif (struct=='unknown' or is_generic(n)) and small_text:
                reason='deep_small_unclassified_or_empty_detail'
        if reason:
            reasons[key]=reason;evidence[key]={'original_level':level,'bbox':list(n['bbox']),'struct':struct,'subtype':sub,'reason':reason}
    for n in sorted(tree['nodes'],key=lambda n:(-depths[n['node_id']],n['node_id'])):
        key=n['node_id'];kids=children.get(key,[])
        candidate_container=is_generic(n) or n.get('struct') in {'unknown','list'} or n.get('subtype') in {'listitem','tbody','thead','tfoot'}
        if depths[key]>=4 and kids and candidate_container and all(k['node_id'] in reasons for k in kids):
            reasons[key]='deep_wrapper_contains_only_low_detail'
            evidence[key]={'original_level':depths[key]+1,'bbox':list(n['bbox']),'struct':n.get('struct'),'subtype':n.get('subtype'),'reason':reasons[key]}
    return reasons,evidence,{'policy':'strict_original_l5_low_detail_v1','counts_by_reason':dict(Counter(reasons.values())),
        'counts_by_original_level':dict(Counter(str(v['original_level']) for v in evidence.values())),
        'selected':len(reasons),'targets_are_omitted_not_declared_invisible':True}
