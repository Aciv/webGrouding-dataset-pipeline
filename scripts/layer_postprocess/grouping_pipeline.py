"""Rule normalization followed by one restricted region-grouping request per observation."""
import json
from pathlib import Path
import shutil
import time
from rules import LEVELS, validate
from normalize import normalize
from frontier import large_region_requirements
from grouping import candidates, apply_decisions
from grouping_vlm import Client
from vlm import ResponseError
from storage import write, image_path

PIPELINE_VERSION = 'visual_layers_grouping_v6'
GROUP_CACHE_VERSIONS = {'visual_layers_grouping_v5', PIPELINE_VERSION}


def process(file, source, output, config, dry_run, reuse_edits=None):
    """Keep source read-only; isolate failures and distinguish structural acceptance from truth."""
    start = time.monotonic()
    relative = file.relative_to(source) if source.is_dir() else Path(file.name)
    destination = output/relative.parent
    audit = {'source': str(file), 'status': 'failed', 'calls': [], 'timings': {},
        'postprocess_version': PIPELINE_VERSION, 'mode': 'candidate_grouping_only'}
    try:
        original = json.loads(file.read_text())
        validate(original)
        image = image_path(file, original)
        began = time.monotonic()
        tree, rule_ops, summary = normalize(original, config.get('rules', {}))
        policy = {**config.get('rules', {}), **config.get('grouping', {})}
        proposed = candidates(tree, policy)
        audit.update(before=len(original['nodes']), rule_operations=rule_ops, rule_summary=summary,
            group_candidates=proposed, level_definitions=LEVELS)
        audit['timings']['rules_seconds'] = round(time.monotonic()-began, 4)
        write(destination/'rule-hierarchy.json', tree)
        write(destination/'candidate-hierarchy.json', tree)
        if dry_run:
            audit['status'] = 'proposed'
        else:
            if reuse_edits:
                previous = json.loads(reuse_edits.read_text())
                if previous.get('postprocess_version') not in GROUP_CACHE_VERSIONS or Path(previous['source']).resolve() != file.resolve():
                    raise ValueError('Cached grouping version/source mismatch')
                if previous.get('group_candidates') != proposed or previous.get('rule_operations') != rule_ops:
                    raise ValueError('Cached candidate preparation changed; run without --reuse-edits')
                response = previous['group_response']
                audit['reused_group_report'] = str(reuse_edits)
            elif proposed:
                client = Client(config['api'], original_tree=original)
                print(json.dumps({'type': 'vlm_started', 'source': str(file), 'phase': 'group_candidates', 'candidates': len(proposed)}), flush=True)
                began = time.monotonic()
                try:
                    response, timing = client.call(tree, image, {'candidates': proposed})
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
            result, operations, rejected, unresolved = apply_decisions(tree, proposed, response, policy.get('depth_overflow_policy', 'reject'))
            # No full second normalizer: only explicitly audited >L4 leaves are omitted.
            validate(result, 4)
            checks = large_region_requirements(result, config.get('rules', {}))
            # No approval means no change, not a veto of the rule baseline.
            blockers = [f'group_decision_rejected:{r["error"]}' for r in rejected]
            # Region combination is not authorized to exempt/decompose oversized roots.
            size_warnings = [f'oversized_l1_retained:{c["node_id"]}' for c in checks if c['status'] == 'requires_visual_decision']
            audit.update(applied_group_operations=[op for op in operations if op['op'] == 'group'],
                depth_pruning_operations=[op for op in operations if op['op'] == 'drop'],
                rejected_operations=rejected,
                unresolved_candidates=unresolved, large_region_checks=checks,
                grouping_warnings=[f'candidate_unchanged_without_approval:{key}' for key in unresolved]+size_warnings,
                oversized_region_warnings=size_warnings,
                after=len(result['nodes']), review_blockers=blockers,
                acceptance_basis='local_structure_and_candidate_decisions_not_whole_tree_visual_quality')
            result.update(rule_version=PIPELINE_VERSION, parent_basis='rule_tree_vlm_candidate_grouping',
                annotation_status='rule_candidate_vlm_grouped_unverified')
            result['postprocess'] = {'schema': 'four_visual_levels_v1', 'mode': 'candidate_grouping_only',
                'levels': LEVELS, 'operations': rule_ops+operations, 'training_ignore_required': True,
                'review_model': config['api']['model'] if proposed else None,
                'group_decisions': response['groups'], 'whole_tree_vlm_verified': False,
                'depth_limit': 4, 'depth_overflow_policy': policy.get('depth_overflow_policy', 'reject'),
                'review_required': bool(blockers), 'warnings': size_warnings}
            write(destination/'candidate-hierarchy.json', result)
            # Always export the safe derived pair, including explicitly flagged review candidates.
            shutil.copyfile(image, destination/image.name)
            write(destination/'hierarchy.json', result)
            audit['status'] = 'needs_review' if blockers else 'accepted'
    except Exception as error:
        audit['error'] = str(error) if isinstance(error, (ValueError, RuntimeError, KeyError)) else type(error).__name__
    audit['seconds'] = round(time.monotonic()-start, 3)
    write(destination/'layer-review.json', audit)
    return {k: audit[k] for k in ('source', 'status', 'seconds', 'timings', 'error', 'before', 'after', 'rule_summary', 'review_blockers') if k in audit}
