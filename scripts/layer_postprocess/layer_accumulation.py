"""Bottom-up L5+ accumulation into a four-level main tree with a non-training fifth archive."""
from collections import Counter,defaultdict
from copy import deepcopy
import math
from rules import validate,overlap

DEFAULTS = {
    'fold_duplicate_iou': .9, 'fold_duplicate_size_ratio': .95,
    'fold_existing_coverage': .95,
    'fold_reference_height_width_cap': 1.5,
    'fold_min_area_ratio': .0001, 'fold_min_width_ratio': .03, 'fold_min_height_ratio': .03,
    'fold_small_max_side_ratio': .08,
    'fold_size_similarity_ratio': 1.25, 'fold_frequency_threshold': 20,
    'fold_frequency_step': 20, 'fold_frequency_max_penalty': 4,
    'fold_dense_count': 16, 'fold_overlap_coverage': .8, 'fold_overlap_fraction': .2,
    'fold_sparse_upper_count': 2, 'fold_sparse_min_children': 4,
}

RANK={'image':90,'media':90,'control':85,'text':80,'graphic':75,'table':70,'list':60,'region':40,'block':20,'unknown':10}


def near_box(a,b,policy):
    """Match near-identical visual boxes, not ordinary containment inside a broad container."""
    return overlap(a,b)>=policy.get('fold_duplicate_iou',.9) and all(min(a[i],b[i])/max(a[i],b[i])>=policy.get('fold_duplicate_size_ratio',.95) for i in (2,3))


def covered_fraction(inner,outer):
    """Fraction of the inner xywh box covered by outer; no pixel visibility inference."""
    area=max(0,min(inner[0]+inner[2],outer[0]+outer[2])-max(inner[0],outer[0]))*max(0,min(inner[1]+inner[3],outer[1]+outer[3])-max(inner[1],outer[1]))
    return area/(inner[2]*inner[3])


def same_object_detail(node,owner):
    """An existing atomic L4 object represents its inner label/icon, not unrelated objects."""
    if covered_fraction(node['bbox'],owner['bbox'])<.95:return False
    if owner.get('struct') in {'image','media','graphic','control'}:
        return node.get('struct')!='control'  # Preserve a distinct overlay/control action.
    if owner.get('struct')=='text':
        return node.get('struct') in {'text','graphic','unknown'}
    if owner.get('subtype') in {'table_cell','td','th','cell','table_row'}:
        return node.get('struct') in {'text','graphic','unknown'} and node['bbox'][2]*node['bbox'][3]/(owner['bbox'][2]*owner['bbox'][3])<=.2
    return False  # A generic enclosing block is NOT proof an independent object is represented.


def size_groups(nodes,policy=None):
    """Count similar width AND height across classes; complete-link bounds prevent size chaining."""
    policy=policy or {};tolerance=policy.get('fold_size_similarity_ratio',1.25)
    if not isinstance(tolerance,(int,float)) or not 1<=tolerance<=2:raise ValueError('fold_size_similarity_ratio must be 1..2')
    groups=[]
    for n in sorted(nodes,key=lambda n:(n['bbox'][2]*n['bbox'][3],n['bbox'][2],n['bbox'][3],n['node_id'])):
        w,h=n['bbox'][2:];matches=[]
        for i,g in enumerate(groups):
            if max(w,g['max_w'])/min(w,g['min_w'])<=tolerance and max(h,g['max_h'])/min(h,g['min_h'])<=tolerance:
                distance=abs(math.log(w/((g['min_w']+g['max_w'])/2)))+abs(math.log(h/((g['min_h']+g['max_h'])/2)))
                matches.append((distance,i))
        if matches:
            g=groups[min(matches)[1]];g['min_w']=min(w,g['min_w']);g['max_w']=max(w,g['max_w']);g['min_h']=min(h,g['min_h']);g['max_h']=max(h,g['max_h'])
        else:
            g={'group_id':f's{len(groups)+1:03d}','min_w':w,'max_w':w,'min_h':h,'max_h':h,'members':[],'positions':set()};groups.append(g)
        g['members'].append(n['node_id']);g['positions'].add(tuple(round(v,1) for v in n['bbox']))
    lookup={};limit=policy.get('fold_frequency_threshold',20)
    for g in groups:
        g['count']=len(g.pop('positions'))
        g['penalty']=min(policy.get('fold_frequency_max_penalty',4),1+max(0,g['count']-limit)/max(1,policy.get('fold_frequency_step',20)))
        for key in g['members']:lookup[key]=g
    return groups,lookup


def tiny_decision(node,capture,group,policy):
    """Archive only when area AND width AND height ratios are small, with bounded size-frequency penalty."""
    ref_w=capture['width'];ref_h=min(capture['height'],ref_w*policy.get('fold_reference_height_width_cap',1.5))
    w,h=node['bbox'][2:];wr,hr=w/ref_w,h/ref_h;ar=w*h/(ref_w*ref_h);p=group['penalty']
    width_limit=min(policy.get('fold_small_max_side_ratio',.08),policy.get('fold_min_width_ratio',.03)*p)
    height_limit=min(policy.get('fold_small_max_side_ratio',.08),policy.get('fold_min_height_ratio',.03)*p)
    area_limit=policy.get('fold_min_area_ratio',.0001)*p*p
    tiny=wr<width_limit and hr<height_limit and ar<area_limit
    return tiny,{'width_ratio':wr,'height_ratio':hr,'area_ratio':ar,'reference_width':ref_w,'reference_height':ref_h,
        'similar_size_group':group['group_id'],'similar_size_count':group['count'],'penalty':p,
        'effective_width_limit':width_limit,'effective_height_limit':height_limit,'effective_area_limit':area_limit}


def accumulation_metrics(tree,policy=None):
    """Measure sibling density/overlap and sparse upper structure; these only trigger proposals."""
    policy=policy or {};depths=validate(tree);index={n['node_id']:n for n in tree['nodes']};families=defaultdict(list)
    for n in tree['nodes']:families[n['parent_id']].append(n)
    result={}
    for parent,kids in families.items():
        if parent is None or len(kids)<2:continue
        pairs=0;overlaps=0
        for i,a in enumerate(kids):
            for b in kids[i+1:]:
                pairs+=1
                overlaps+=int(max(covered_fraction(a['bbox'],b['bbox']),covered_fraction(b['bbox'],a['bbox']))>=policy.get('fold_overlap_coverage',.8))
        fraction=overlaps/max(1,pairs);upper_count=len(families[index[parent]['parent_id']]);reasons=[]
        if len(kids)>=policy.get('fold_dense_count',16):reasons.append('many_elements')
        if overlaps and fraction>=policy.get('fold_overlap_fraction',.2):reasons.append('many_overlapping_elements')
        if upper_count<policy.get('fold_sparse_upper_count',2) and len(kids)>=policy.get('fold_sparse_min_children',4):reasons.append('sparse_upper_region')
        result[parent]={'child_level':depths[parent]+2,'count':len(kids),'overlapping_pairs':overlaps,'pair_count':pairs,'overlap_fraction':fraction,'upper_sibling_count':upper_count,'triggers':reasons}
    return result


def accumulate_to_fourth(tree,original=None,policy=None):
    """Keep L1-L3; backfill distinct effective deep boxes into L4, preserving the rest in L5 archive.

    The archive is separate from the parent-depth main tree. It uses owner_id associations,
    retains original parent/level/bbox and is excluded from training by default.
    """
    policy=policy or {};original=original or tree;result=deepcopy(tree);depths=validate(tree);original_depths=validate(original)
    index={n['node_id']:n for n in tree['nodes']};raw={n['node_id']:n for n in original['nodes']};families=defaultdict(list);archive={};alias={};coverage_owner={};bases=set()
    def archive_node(n,reason,owner=None,extra=None):
        key=n['node_id'];source=raw.get(key,n);saved={k:deepcopy(v) for k,v in source.items() if k not in {'parent_id','layer'}}
        saved.update(display_level=5,original_parent_id=source.get('parent_id'),original_level=original_depths.get(key,depths.get(key,0))+1,
            owner_id=owner,archive_reason=reason,excluded_from_training=True)
        if extra:saved.update(extra)
        archive[key]=saved
    for n in tree['nodes']:
        key=n['node_id']
        if depths[key]<3:continue
        chain=[];p=n['parent_id']
        while p is not None:chain.append(index[p]);p=index[p]['parent_id']
        parent=next(v['node_id'] for v in chain if depths[v['node_id']]==2)
        fourth=next((v for v in chain if depths[v['node_id']]==3),None)
        if depths[key]==3:bases.add(key)
        if depths[key]>=4 and fourth and same_object_detail(n,fourth):
            archive_node(n,'already_represented_by_l4_object',fourth['node_id']);coverage_owner[key]=fourth['node_id'];continue
        families[parent].append(n)
    child_counts=Counter(n['parent_id'] for n in tree['nodes'])
    def same_single_figure(a,b):
        figure,content=(a,b) if a.get('subtype')=='figure' else (b,a)
        if figure.get('subtype')!='figure' or depths[figure['node_id']]!=3 or child_counts[figure['node_id']]!=1 or content.get('struct') not in {'image','media','graphic'}:
            return False
        key=content.get('parent_id');seen=set();related=False
        while key is not None and key not in seen:
            seen.add(key)
            if key==figure['node_id']:related=True;break
            key=index[key]['parent_id'] if key in index else None
        return related and overlap(figure['bbox'],content['bbox'])>=.75 and all(min(figure['bbox'][i],content['bbox'][i])/max(figure['bbox'][i],content['bbox'][i])>=.75 for i in (2,3))
    chosen=[];base_backed=set()
    for parent,items in families.items():
        representatives=[];members=defaultdict(list)
        for n in sorted(items,key=lambda n:(-RANK.get(n.get('struct'),0),-depths[n['node_id']],n['node_id'])):
            match=next((v for v in representatives if near_box(n['bbox'],v['bbox'],policy) or same_single_figure(n,v)),None)
            if match:
                alias[n['node_id']]=match['node_id'];members[match['node_id']].append(n['node_id']);archive_node(n,'near_duplicate_visual_box' if near_box(n['bbox'],match['bbox'],policy) else 'same_object_l4_representative',match['node_id'])
            else:representatives.append(n);members[n['node_id']].append(n['node_id'])
        for n in representatives:
            if any(k in bases for k in members[n['node_id']]):base_backed.add(n['node_id'])
            chosen.append((parent,n))
    groups,lookup=size_groups([n for _,n in chosen],policy);main=[deepcopy(n) for n in tree['nodes'] if depths[n['node_id']]<3];supplemented=[];replaced=[];tiny=[];trail=[]
    # Preserve the existing L4 frontier, including identity-equivalent representative swaps.
    # Coverage is geometric policy, not a claim that two objects have the same function.
    existing=defaultdict(list);accepted=defaultdict(list)
    for parent,n in chosen:
        if n['node_id'] in base_backed:existing[parent].append(n)
    coverage=policy.get('fold_existing_coverage',.95)
    if not isinstance(coverage,(int,float)) or not 0 < coverage <= 1:
        raise ValueError('fold_existing_coverage must be in (0, 1]')
    # Shallower original branches first: do not flatten a wrapper and all of its
    # contents into sibling training targets. Stable order is independent of input order.
    for parent,n in sorted(chosen,key=lambda item:(depths[item[1]['node_id']],-item[1]['bbox'][2]*item[1]['bbox'][3],item[1]['node_id'])):
        key=n['node_id'];small,measurement=tiny_decision(n,tree['capture'],lookup[key],policy)
        if key not in base_backed:
            owners=[owner for owner in existing[parent] if covered_fraction(n['bbox'],owner['bbox'])>=coverage]
            if owners:
                owner=min(owners,key=lambda owner:(owner['bbox'][2]*owner['bbox'][3],owner['node_id']))
                archive_node(n,'covered_by_existing_l4',owner['node_id'],{'coverage_fraction':covered_fraction(n['bbox'],owner['bbox'])})
                continue
        if key not in base_backed and small:
            archive_node(n,'proportionally_tiny_detail',parent,{'size_evidence':measurement});tiny.append(key);continue
        if key not in base_backed:
            owners=[owner for owner in accepted[parent] if max(covered_fraction(n['bbox'],owner['bbox']),covered_fraction(owner['bbox'],n['bbox']))>=coverage]
            if owners:
                owner=owners[0]
                archive_node(n,'nested_supplement_not_selected',owner['node_id'])
                continue
            accepted[parent].append(n)
        value=deepcopy(n);value.update(parent_id=parent,layer=3)
        value['accumulation_source']={'previous_parent_id':n['parent_id'],'previous_level':depths[key]+1,'original_level':original_depths.get(key,depths[key])+1}
        main.append(value)
        if depths[key]>=4:
            (replaced if key in base_backed else supplemented).append(key)
        if n['parent_id']!=parent:trail.append({'op':'reparent','node':key,'parent':parent,'reason':'rule_bottom_up_accumulate_at_l4'})
    main_index={n['node_id']:n for n in main}
    # Preserve source-deep nodes omitted by preliminary wrapper/geometry normalization too.
    for key,n in raw.items():
        if original_depths[key]>=4 and key not in main_index and key not in archive:
            archive_node(n,'source_detail_not_selected_for_main')
    def resolve_owner(key):
        seen=set()
        while key is not None and key not in seen:
            seen.add(key)
            if key in main_index:return key,'recorded_identity_or_ancestor'
            if key in alias:key=alias[key];continue
            if key in coverage_owner:key=coverage_owner[key];continue
            if key in archive and archive[key].get('owner_id') is not None:key=archive[key]['owner_id'];continue
            n=index.get(key,raw.get(key));key=n.get('parent_id') if n else None
        return None,'unresolved'
    for a in archive.values():
        owner,basis=resolve_owner(a.get('owner_id') or a['original_parent_id']);a['owner_id']=owner;a['owner_basis']=basis
    result['nodes']=main;validate(result,4)
    decision_map={d['node_id']:d for d in result.get('decisions',[])}
    for a in archive.values():
        decision_map[a['node_id']]={'node_id':a['node_id'],'status':'archived_detail','reason':a['archive_reason'],'represented_by':a.get('owner_id')}
    result['decisions']=list(decision_map.values())
    result['detail_archive']={'schema':'nontraining_visual_details_v1','display_level':5,'excluded_from_training_by_default':True,
        'relation':'owner_id is an association, not a fifth-level training parent edge','nodes':list(archive.values())}
    result['accumulation']={'method':'strict_l4_missing_frontier_backfill_v2','supplemented_to_l4':supplemented, 'replaced_existing_l4':replaced,
        'tiny_archived':tiny,'size_groups':groups,'density':accumulation_metrics(result,policy),
        'counts':{'main_nodes':len(main),'l4_nodes':sum(n['layer']==3 for n in main),'supplemented':len(supplemented), 'replaced_existing_l4':len(replaced),
            'archive_nodes':len(archive),'already_represented':sum(a['archive_reason']=='already_represented_by_l4_object' for a in archive.values()),
            'covered_by_existing_l4':sum(a['archive_reason']=='covered_by_existing_l4' for a in archive.values()),
            'nested_supplements_archived':sum(a['archive_reason']=='nested_supplement_not_selected' for a in archive.values()),
            'near_duplicates':sum(a['archive_reason']=='near_duplicate_visual_box' for a in archive.values()),'tiny_archived':len(tiny)}}
    stats=result.setdefault('stats',{});stats.update(retained=len(main),detail_archive=len(archive),max_layer=max((n['layer'] for n in main),default=-1),roots=sum(n['parent_id'] is None for n in main))
    for field in ('layer','kind','struct','disposition'):stats['by_'+field]=dict(Counter(str(n.get(field)) for n in main))
    return result,trail,result['accumulation']['counts']


def refresh_archive_owners(tree):
    """Repair non-training detail associations after upper/pixel region replacements."""
    index={n['node_id']:n for n in tree['nodes']};depths=validate(tree)
    for detail in tree.get('detail_archive',{}).get('nodes',[]):
        owner=index.get(detail.get('owner_id'))
        if owner and covered_fraction(detail['bbox'],owner['bbox'])>=.9:continue
        matches=[n for n in tree['nodes'] if covered_fraction(detail['bbox'],n['bbox'])>=.95]
        winner=min(matches,key=lambda n:(-depths[n['node_id']],n['bbox'][2]*n['bbox'][3],n['node_id'])) if matches else None
        detail['owner_id']=winner['node_id'] if winner else None
        detail['owner_basis']='geometry_containment_unverified' if winner else 'unresolved'
    return tree
