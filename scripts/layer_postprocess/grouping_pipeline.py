"""Rule normalization followed by one restricted region-grouping request per observation."""
import json
from pathlib import Path
import shutil
import threading
import time
from rules import LEVELS, validate
from normalize import normalize
from frontier import large_region_requirements
from grouping import candidates, apply_decisions
from semantic_cleanup import top_down_cleanup
from layer_accumulation import accumulate_to_fourth, refresh_archive_owners, DEFAULTS as ACCUMULATION_DEFAULTS
from pixel_regions import pixel_candidates, apply_pixel_decisions
from grouping_vlm import Client
from vlm import ResponseError
from storage import write, image_path, validate_image

PIPELINE_VERSION = 'visual_layers_fusion_clean5_v13'
GROUP_CACHE_VERSIONS = {PIPELINE_VERSION}
_PIXEL_SEMAPHORES = {}
_PIXEL_SEMAPHORE_LOCK = threading.Lock()


def _pixel_semaphore(limit):
    """Bound photo-decoding fallback independently from the overall VLM concurrency."""
    if not limit:
        return None
    with _PIXEL_SEMAPHORE_LOCK:
        return _PIXEL_SEMAPHORES.setdefault(limit, threading.BoundedSemaphore(limit))


def process(file, source, output, config, dry_run, reuse_edits=None,
            image_override=None, relative_override=None, copy_image=True):
    """Keep source read-only; isolate failures and distinguish structural acceptance from truth.

    Index-driven callers may pass the original screenshot explicitly and choose a logical
    output path, so a derived hierarchy never has to live beside a copied image.
    """
    start = time.monotonic()
    file = Path(file)
    relative = (Path(relative_override) if relative_override is not None
                else file.relative_to(source) if Path(source).is_dir() else Path(file.name))
    destination = Path(output)/relative.parent
    audit = {'source': str(file), 'status': 'failed', 'calls': [], 'timings': {},
        'postprocess_version': PIPELINE_VERSION, 'mode': 'replace_upper_region'}
    try:
        original = json.loads(file.read_text())
        validate(original)
        image = (validate_image(image_override, original) if image_override is not None
                 else image_path(file, original))
        began = time.monotonic()
        policy = {**ACCUMULATION_DEFAULTS, **config.get('rules', {}), **config.get('grouping', {}), **config.get('pixels', {}), **config.get('accumulation', {}),
            'preserve_meaningful_deep': True, 'conservative_grouping': True, 'group_action': 'replace_upper', 'deep_noise_mode': 'off',
            'accumulation_mode': True, 'prefer_existing_region_replacement': True, 'density_triggered_only': True}
        audit['effective_rule_policy'] = policy
        prepared, pre_ops, pre_summary = top_down_cleanup(original, policy, compress_only=True)
        audit['top_down_cleanup'] = pre_summary
        tree, norm_ops, summary = normalize(prepared, policy)
        # Candidate generation and model execution retain the complete normalized tree.
        # The four-layer preview is display-only: never feed its archive back into fusion.
        fusion_depths = validate(tree)
        write(destination/'fusion-input-hierarchy.json', tree)
        preview, preview_fold_ops, fold_summary = accumulate_to_fourth(tree, original, policy)
        audit['fusion_input'] = {'nodes': len(tree['nodes']),
            'levels': max(fusion_depths.values(), default=-1)+1,
            'deep_nodes_available': sum(depth >= 4 for depth in fusion_depths.values()),
            'path': 'fusion-input-hierarchy.json'}
        audit['processing_order'] = ['normalize_full_tree', 'propose_full_tree_fusion',
            'apply_approved_bottom_up_replacements', 'finalize_l4_and_archive_l5']
        audit['preview_accumulation_operations'] = preview_fold_ops
        audit['layer_accumulation'] = preview['accumulation']
        audit['layer_accumulation_summary'] = fold_summary
        rule_ops = pre_ops+norm_ops
        summary['before_accumulation_levels'] = summary['after_levels']
        summary['after_levels'] = max(validate(preview).values(), default=-1)+1
        summary['after'] = len(preview['nodes'])
        pixel_limit = policy.get('pixel_concurrency', 0)
        if type(pixel_limit) is not int or pixel_limit < 0:
            raise ValueError('pixel_concurrency must be a nonnegative integer')
        semaphore = _pixel_semaphore(pixel_limit)
        if semaphore is None:
            pixel_proposed, pixel_evidence = pixel_candidates(tree, image, policy)
        else:
            with semaphore:
                pixel_proposed, pixel_evidence = pixel_candidates(tree, image, policy)
        # Pixel partitions and grouping cannot both reorganize the same root branch.
        pixel_roots = {p['root_id'] for p in pixel_proposed}
        index = {n['node_id']: n for n in tree['nodes']}
        def under_pixel_root(key):
            while key is not None:
                if key in pixel_roots: return True
                key = index[key]['parent_id']
            return False
        proposed = [g for g in candidates(tree, policy) if not any(under_pixel_root(k) for k in g['members'])]
        audit.update(pixel_candidates=pixel_proposed, pixel_evidence=pixel_evidence)
        audit.update(before=len(original['nodes']), rule_operations=rule_ops, rule_summary=summary,
            group_candidates=proposed, level_definitions=LEVELS)
        audit['timings']['rules_seconds'] = round(time.monotonic()-began, 4)
        audit['deep_group_candidates'] = sum(g['replacement_plan']['upper_level'] >= 4 for g in proposed)
        write(destination/'rule-hierarchy.json', preview)
        write(destination/'candidate-hierarchy.json', preview)
        if dry_run:
            audit['status'] = 'proposed'
        else:
            if reuse_edits:
                previous = json.loads(reuse_edits.read_text())
                if previous.get('postprocess_version') not in GROUP_CACHE_VERSIONS or Path(previous['source']).resolve() != file.resolve():
                    raise ValueError('Cached grouping version/source mismatch')
                if previous.get('group_candidates') != proposed or previous.get('rule_operations') != rule_ops or previous.get('pixel_candidates', []) != pixel_proposed:
                    raise ValueError('Cached candidate preparation changed; run without --reuse-edits')
                response = previous['group_response']
                audit['reused_group_report'] = str(reuse_edits)
            elif proposed or pixel_proposed:
                client = Client(config['api'], original_tree=original)
                print(json.dumps({'type': 'vlm_started', 'source': str(file), 'phase': 'group_candidates', 'candidates': len(proposed)}), flush=True)
                began = time.monotonic()
                try:
                    response, timing = client.call(tree, image, {'candidates': proposed, 'pixel_candidates': pixel_proposed})
                except ResponseError as error:
                    audit['calls'].append({'phase': 'group_candidates', **error.timing, 'status': 'invalid_response'})
                    audit['invalid_model_response'] = error.content
                    raise
                except Exception:
                    audit['calls'].append({'phase': 'group_candidates', 'status': 'failed', 'seconds': round(time.monotonic()-began, 3)})
                    raise
                audit['calls'].append({'phase': 'group_candidates', **timing})
                print(json.dumps({'type': 'vlm_finished', 'source': str(file), 'phase': 'group_candidates', **timing}), flush=True)
            else:
                response = {'groups': []}
                audit['api_skipped'] = 'no_group_candidates'
            audit['group_response'] = response
            if not isinstance(response, dict) or set(response)-{'groups', 'pixel_splits'}:
                raise ValueError('Only groups and pixel_splits are allowed')
            # No depth pruning here: all deep details must reach the final archive stage.
            result, operations, rejected, unresolved = apply_decisions(tree, proposed,
                {'groups': response.get('groups', [])}, 'reject', semantic_policy=policy)
            result, pixel_ops, pixel_rejected = apply_pixel_decisions(result, pixel_proposed, response.get('pixel_splits', []))
            operations += pixel_ops
            rejected += pixel_rejected
            result, compressed, compression_summary = top_down_cleanup(result, policy, compress_only=True)
            operations += compressed
            audit.update(pixel_operations=pixel_ops, post_group_cleanup=compression_summary)
            audit['pre_archive_fusion'] = {'nodes': len(result['nodes']),
                'levels': max(validate(result).values(), default=-1)+1,
                'applied_replacements': len(result.get('region_replacements', []))}
            # Canonical cleaned full tree for true L1-L5 export; never reconstruct it
            # by concatenating archive associations to a four-level training tree.
            write(destination/'fused-hierarchy.json', result)
            result, fold_ops, fold_summary = accumulate_to_fourth(result, original, policy)
            operations += fold_ops
            audit['layer_accumulation'] = result['accumulation']
            audit['layer_accumulation_summary'] = fold_summary
            result = refresh_archive_owners(result)
            final_depths = validate(result)
            remaining_deep = [k for k,depth in final_depths.items() if depth >= 4]
            audit['meaningful_deep_unresolved'] = remaining_deep
            checks = large_region_requirements(result, config.get('rules', {}))
            # No approval means no change, not a veto of the rule baseline.
            blockers = [f'group_decision_rejected:{r["error"]}' for r in rejected]
            if remaining_deep:
                blockers.append(f'meaningful_depth_unresolved:{len(remaining_deep)}')
            # Region combination is not authorized to exempt/decompose oversized roots.
            size_warnings = [f'oversized_l1_retained:{c["node_id"]}' for c in checks if c['status'] == 'requires_visual_decision']
            audit.update(applied_group_operations=[op for op in operations if op['op'] == 'group'],
                depth_pruning_operations=[op for op in operations if op['op'] == 'drop'],
                postgroup_rule_operations=[op for op in operations if op['op'] not in ('group', 'pixel_region')],
                applied_upper_replacements=result.get('region_replacements', []),
                rejected_operations=rejected,
                unresolved_candidates=unresolved, large_region_checks=checks,
                grouping_warnings=[f'candidate_unchanged_without_approval:{key}' for key in unresolved]+size_warnings,
                oversized_region_warnings=size_warnings,
                after=len(result['nodes']), review_blockers=blockers,
                acceptance_basis='local_structure_and_candidate_decisions_not_whole_tree_visual_quality')
            result.update(rule_version=PIPELINE_VERSION, parent_basis='rule_tree_vlm_candidate_grouping',
                annotation_status='rule_candidate_vlm_grouped_unverified')
            result['postprocess'] = {'schema': 'four_level_main_with_l5_archive_v1', 'mode': 'replace_upper_region',
                'levels': LEVELS, 'operations': rule_ops+operations, 'training_ignore_required': True,
                'review_model': config['api']['model'] if proposed or pixel_proposed else None,
                'group_decisions': response.get('groups', []), 'pixel_decisions': response.get('pixel_splits', []),
                'training_eligible': not remaining_deep and not blockers,
                'default_training_levels': [1,2,3,4], 'detail_archive_level': 5, 'whole_tree_vlm_verified': False,
                'depth_limit': 4, 'depth_overflow_policy': 'archive',
                'review_required': bool(blockers or size_warnings), 'warnings': size_warnings}
            write(destination/'candidate-hierarchy.json', result)
            # Legacy processing exports the safe pair; index mode keeps images in the source run.
            if copy_image:
                shutil.copyfile(image, destination/image.name)
            write(destination/'hierarchy.json', result)
            audit['status'] = 'needs_review' if blockers else 'accepted'
    except Exception as error:
        audit['error'] = f'{type(error).__name__}: {error}'
    audit['seconds'] = round(time.monotonic()-start, 3)
    write(destination/'layer-review.json', audit)
    return {k: audit[k] for k in ('source', 'status', 'seconds', 'timings', 'error', 'before', 'after', 'rule_summary', 'review_blockers') if k in audit}
