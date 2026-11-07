"""Select detail-training crop views without changing observation quality or page-cut supervision."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
import json
import math
from pathlib import Path
from scripts.offline_postprocess.domain import area, intersection
from scripts.offline_postprocess.infrastructure import read_jsonl, write_json, write_jsonl, sha256_file
from scripts.offline_postprocess.slicing_statistics import rectangle_union_area

RULE = 'detail_crop_coverage_box_pixels_v2'
PIXEL_POLICY = {'maximum_uninformative_box_ratio': 0.6, 'max_channel_range': 4,
                'comparison': 'discard if ratio >= threshold', 'pixel_basis': 'original_rgb_before_overlays',
                'denominator': 'all_final_positive_boxes_in_crop', 'empty_targets': 'not_evaluated'}

def target_box_pixel_evidence(image, crop):
    """Check original RGB inside crop-local xyxy positives; count each box, including overlaps, once.

    Uniform RGB is pixel evidence only, not a claim about DOM visibility or loading state.
    Invalid/outside boxes fail validation rather than being treated as blank.
    """
    records = []
    cx, cy = crop['crop_bbox'][:2]
    for target in crop['targets']:
        x1,y1,x2,y2 = target['bbox']
        box = (math.floor(cx+x1), math.floor(cy+y1), math.ceil(cx+x2), math.ceil(cy+y2))
        if box[0]<0 or box[1]<0 or box[2]>image.width or box[3]>image.height or box[2]<=box[0] or box[3]<=box[1]:
            raise ValueError('Invalid target pixel region: '+crop['crop_id'])
        with image.crop(box) as patch:
            maximum_range = max(high-low for low,high in patch.getextrema())
        if maximum_range <= PIXEL_POLICY['max_channel_range']:
            records.append({'global_node_id':target['global_node_id'], 'max_channel_range':maximum_range})
    count = len(crop['targets'])
    ratio = len(records)/count if count else None
    return {'checked': bool(count), 'total_boxes':count, 'uninformative_boxes':len(records),
            'uninformative_box_ratio':ratio, 'invalid': bool(count and len(records)*5 >= count*3),
            'uniform_boxes':records}



def target_union_coverage(crop):
    """Measure union of all positive xyxy boxes in crop-local pixels; ignore/archive boxes do not count."""
    box = crop['crop_bbox']
    local = [0, 0, box[2]-box[0], box[3]-box[1]]
    if area(local) <= 0:
        raise ValueError('Invalid crop area')
    return min(1.0, rectangle_union_area([intersection(t['bbox'], local) for t in crop['targets']])/area(local))


def select_detail_crops(index_path, output, minimum_coverage=0.2, resume=False):
    """Derive flagged/all and trainable-only crop manifests from a validated final index.

    Original graph/audit/source files remain immutable references. Status and split
    manifests are unchanged even if every detail crop of an observation is withheld.
    Publish a new final index last; source geometry validation remains separately linked.
    """
    if not math.isfinite(minimum_coverage) or not 0 <= minimum_coverage <= 1:
        raise ValueError('minimum coverage must be in [0,1]')
    index_path, output = Path(index_path).resolve(), Path(output).resolve()
    source = json.loads(index_path.read_text())
    if (source.get('schema') != 'gui_component_dataset_index_v1'
            or source.get('stage') != 'final' or source.get('publication_status') != 'validated'):
        raise ValueError('Validated component final index required')
    if source.get('detail_training_selection'):
        raise ValueError('Select from the original validated generation index, not an already selected index')
    if output == index_path.parent or output in index_path.parents or index_path.parent in output.parents:
        raise ValueError('Detail selection must use a separate sibling output directory')
    raw_root = Path(source['source_root']).resolve()
    if output == raw_root or output in raw_root.parents or raw_root in output.parents:
        raise ValueError('Detail selection must not modify the raw source tree')
    for artifact in source['artifacts'].values():
        if sha256_file(artifact['path']) != artifact['sha256']:
            raise ValueError('Source artifact changed: '+artifact['path'])
    if not json.loads(Path(source['artifacts']['validation']['path']).read_text()).get('passed'):
        raise ValueError('Source generation validation failed')
    policy = {'rule_version': RULE, 'scope': 'detail_crop', 'minimum_target_union_coverage': minimum_coverage,
              'comparison': 'discard if coverage < threshold; equality is retained',
              'denominator': 'actual_crop_area', 'box_pixel_policy': PIXEL_POLICY, 'source_index_sha256': sha256_file(index_path)}
    final_path = output/'index-final.json'
    if output.exists() and any(output.iterdir()):
        if resume and final_path.is_file():
            previous = json.loads(final_path.read_text())
            if previous.get('detail_training_selection') == policy:
                for artifact in previous['artifacts'].values():
                    if sha256_file(artifact['path']) != artifact['sha256']:
                        raise ValueError('Selection artifact changed; choose another output')
                report = json.loads((output/'reports/detail-selection.json').read_text())
                return {**report, 'resumed': True, 'final_index': str(final_path)}
        raise ValueError('Selection output must be empty or matching --resume output')
    accepted = {o['observation_id']:o for o in source['observations'] if o['status']=='accepted'}
    rows, counts, discarded = [], Counter(), Counter()
    seen = set()
    grouped = defaultdict(list)
    for crop in read_jsonl(source['artifacts']['detection_crops']['path']):
        oid = crop['observation_id']
        if oid not in accepted or crop['status'] != 'accepted' or crop['crop_id'] in seen:
            raise ValueError('Invalid source crop manifest')
        seen.add(crop['crop_id'])
        grouped[oid].append(crop)
    def inspect_observation(item):
        oid, crops = item
        with Image.open(accepted[oid]['source_image']) as original:
            with original.convert('RGB') as image:
                checked = []
                for crop in crops:
                    coverage = target_union_coverage(crop)
                    pixels = target_box_pixel_evidence(image, crop)
                    reasons = (['low_target_union_coverage'] if coverage < minimum_coverage else [])
                    if pixels['invalid']:
                        reasons.append('mostly_uninformative_target_boxes')
                    checked.append({**crop, 'target_union_coverage':coverage, 'box_pixel_evidence':pixels,
                                    'crop_pixel_valid':not pixels['invalid'],
                                    'detail_training_eligible':not reasons, 'detail_training_discarded':bool(reasons),
                                    'detail_training_reason_codes':reasons})
                return checked
    with ThreadPoolExecutor(max_workers=4) as pool:
        for checked in pool.map(inspect_observation, grouped.items()):
            rows.extend(checked)
            for row in checked:
                oid = row['observation_id']
                counts[oid] += int(row['detail_training_eligible'])
                discarded[oid] += int(row['detail_training_discarded'])
    for oid, observation in accepted.items():
        if counts[oid]+discarded[oid] != observation['published_crops']:
            raise ValueError('Source crop count mismatch: '+oid)
    write_jsonl(output/'crops/manifest.jsonl', rows)
    write_jsonl(output/'crops/trainable.jsonl', (r for r in rows if r['detail_training_eligible']))
    write_jsonl(output/'crops/discarded.jsonl', (r for r in rows if r['detail_training_discarded']))
    report = {'schema': 'gui_detail_training_selection_v1', 'policy': policy,
              'candidate_crops': len(rows),
              'discard_reasons':dict(Counter(reason for row in rows for reason in row['detail_training_reason_codes'])),
              'new_pixel_only_discards':sum(r['crop_pixel_valid']==False and r['target_union_coverage']>=minimum_coverage for r in rows), 'trainable_crops': sum(counts.values()),
              'discarded_detail_crops': sum(discarded.values()),
              'observations_with_discarded_detail_crops': sum(n>0 for n in discarded.values()),
              'accepted_observations': len(accepted),
              'observations_with_zero_detail_crops': sum(counts[oid]==0 for oid in accepted),
              'observation_quality_unchanged': True, 'split_supervision_unchanged': True}
    write_json(output/'reports/detail-selection.json', report)
    index = {**source, 'parent': {'path': str(index_path), 'sha256': policy['source_index_sha256']},
             'detail_training_selection': policy,
             'observations': [{**o, 'published_crops': counts[o['observation_id']],
                               'detail_crop_candidates': counts[o['observation_id']]+discarded[o['observation_id']],
                               'discarded_detail_crops': discarded[o['observation_id']],
                               'detection_eligible': bool(counts[o['observation_id']])}
                              if o['status']=='accepted' else o for o in source['observations']],
             'artifacts': dict(source['artifacts'])}
    # A second pass checks persisted selection flags and publication membership.
    persisted = list(read_jsonl(output/'crops/manifest.jsonl'))
    training = list(read_jsonl(output/'crops/trainable.jsonl'))
    valid = (all(r['detail_training_eligible'] == (target_union_coverage(r)>=minimum_coverage and not r['box_pixel_evidence']['invalid'])
                 and r['detail_training_discarded'] != r['detail_training_eligible'] for r in persisted)
             and {r['crop_id'] for r in training} == {r['crop_id'] for r in persisted if r['detail_training_eligible']}
             and [(o['observation_id'],o['status'],o['split_supervision_eligible']) for o in source['observations']]
                 == [(o['observation_id'],o['status'],o['split_supervision_eligible']) for o in index['observations']])
    validation = {'passed': valid, 'scope': 'detail_training_selection',
                  'source_validation': source['artifacts']['validation'], **report}
    write_json(output/'reports/validation.json', validation)
    if not valid:
        raise ValueError('Detail selection validation failed')
    index['artifacts']['source_validation'] = source['artifacts']['validation']
    for key, name in [('detection_crops','crops/trainable.jsonl'), ('detail_crop_manifest','crops/manifest.jsonl'),
                      ('discarded_detail_crops','crops/discarded.jsonl'), ('detail_selection_report','reports/detail-selection.json'),
                      ('validation','reports/validation.json')]:
        path = output/name
        index['artifacts'][key] = {'path':str(path), 'sha256':sha256_file(path)}
    write_json(final_path, index)
    return {**report, 'final_index': str(final_path)}
