"""Previous whole-tree edit/review pipeline retained for historical benchmarks only."""
import json
from pathlib import Path
import shutil
import time
from rules import LEVELS, apply_operations, propose, validate
from normalize import normalize
from model_edits import apply_model_edits
from frontier import large_region_requirements, validate_large_region_review
from vlm import Client, ResponseError
from storage import write, image_path
PIPELINE_VERSION = "visual_layers_vlm_v4"

def process_legacy(file, source, output, config, dry_run, reuse_edits=None):
    """Process one observation in isolation; a rejected/error page never publishes training labels."""
    start = time.monotonic()
    relative = file.relative_to(source) if source.is_dir() else Path(file.name)
    destination = output/relative.parent
    audit = {"source": str(file), "status": "failed", "calls": [], "timings": {}, "postprocess_version": PIPELINE_VERSION}
    def api_call(client, tree, image, proposals, phase, verify=False):
        stage_start = time.monotonic()
        print(json.dumps({"type": "vlm_started", "source": str(file), "phase": phase,
            "nodes": len(tree["nodes"])}), flush=True)
        try:
            response, timing = client.call(tree, image, proposals, verify=verify)
        except ResponseError as error:
            audit["calls"].append({"phase": phase, **error.timing, "status": "invalid_response"})
            audit["invalid_model_response"] = {"phase": phase, "content": error.content}
            raise
        except Exception:
            audit["calls"].append({"phase": phase, "seconds": round(time.monotonic()-stage_start, 3), "status": "failed"})
            raise
        audit["calls"].append({"phase": phase, **timing})
        print(json.dumps({"type": "vlm_finished", "source": str(file), "phase": phase, **timing}), flush=True)
        return response
    try:
        tree = json.loads(file.read_text())
        validate(tree)
        image = image_path(file, tree)
        original = tree
        rule_start = time.monotonic()
        reused_operations = []
        if reuse_edits:
            previous = json.loads(reuse_edits.read_text())
            if previous.get("postprocess_version") != PIPELINE_VERSION:
                raise ValueError("Cached edits use an older normalization version; run from original without --reuse-edits")
            if Path(previous["source"]).resolve() != file.resolve():
                raise ValueError("Reused edit source does not match this observation")
            reused_operations = previous["edit_response"]["operations"]
            if not isinstance(reused_operations, list) or len(reused_operations) > config.get("max_operations", 500):
                raise ValueError("Invalid reused operations")
            # Since v2, paid edits target the prepared candidate, not the original tree.
            preparation = previous.get("edit_base_operations", previous.get("rule_operations", []))
            audit["edit_base_operations"] = preparation
            tree, _ = apply_operations(original, preparation)
            tree, replayed, rejected = apply_model_edits(tree, reused_operations, config.get("rules", {}))
            audit["rejected_operations"] = rejected
            reused_operations = preparation+replayed
            audit["reused_edit_report"] = str(reuse_edits)
            audit["edit_response"] = previous["edit_response"]
        tree, rule_ops, rule_summary = normalize(tree, config.get("rules", {}))
        proposals = propose(tree, config.get("rules", {}))
        audit["timings"]["rules_seconds"] = round(time.monotonic()-rule_start, 4)
        audit.update(before=len(original["nodes"]), proposals=proposals, level_definitions=LEVELS,
            rule_operations=rule_ops, rule_summary=rule_summary)
        write(destination/"candidate-hierarchy.json", tree)
        if dry_run:
            audit["status"] = "proposed"
            write(destination/"candidate-hierarchy.json", tree)
        else:
            client = Client(config["api"], original_tree=original)
            audit["context_mode"] = config["api"].get("context_mode", "delta")
            if reuse_edits:
                result, operations = tree, reused_operations+rule_ops
                review_operations = operations
            else:
                audit["edit_base_operations"] = rule_ops
                context = {"rule_summary": rule_summary, "remaining_candidates": proposals,
                    "applied_rule_edits": rule_ops}
                response = api_call(client, tree, image, context, "edit")
                audit["edit_response"] = response
                ops = response.get("operations")
                if not isinstance(ops, list) or len(ops) > config.get("max_operations", 500):
                    raise ValueError("Invalid/too many operations")
                result, applied, rejected = apply_model_edits(tree, ops, config.get("rules", {}))
                audit["rejected_operations"] = rejected
                # VLM can create a deeper grouping; normalize again rather than failing on depth.
                rule_start = time.monotonic()
                result, followup, followup_summary = normalize(result, config.get("rules", {}))
                audit["timings"]["followup_rules_seconds"] = round(time.monotonic()-rule_start, 4)
                audit["followup_rule_summary"] = followup_summary
                audit["followup_rule_operations"] = followup
                operations = rule_ops+applied+followup
                review_operations = applied+followup
            write(destination/"candidate-hierarchy.json", result)
            review = api_call(client, result, image, {"applied_edits": operations,
                "review_operations": review_operations,
                "large_region_checks": large_region_requirements(result, config.get("rules", {})),
                "rejected_model_instructions": audit.get("rejected_operations", [])}, "verify", verify=True)
            audit["verification"] = review
            size_ok, checks, errors = validate_large_region_review(result, config.get("rules", {}), review)
            audit["large_region_checks"] = checks
            audit["large_region_validation_errors"] = errors
            if review.get("accepted") is not True or not size_ok:
                audit["status"] = "needs_review"
                audit["review_blockers"] = errors or review.get("issues", [])
            else:
                # Depths are computed locally; verification need not transcribe hundreds of nodes.
                levels = review.get("levels", [{"node_id": n["node_id"], "level": n["layer"]+1,
                    "role": LEVELS[str(n["layer"]+1)]} for n in result["nodes"]])
                if not isinstance(levels, list):
                    raise ValueError("Invalid verification levels")
                assignments = {v["node_id"]: v for v in levels}
                if len(assignments) != len(levels) or set(assignments) != {n["node_id"] for n in result["nodes"]}:
                    raise ValueError("VLM level assignments must cover every retained ID exactly once")
                for node in result["nodes"]:
                    value = assignments[node["node_id"]]
                    if type(value.get("level")) is not int or value["level"] != node["layer"]+1 or not isinstance(value.get("role"), str) or not value["role"].strip():
                        raise ValueError("VLM semantic levels disagree with parent depths or lack visual roles")
                result.update(rule_version=PIPELINE_VERSION, parent_basis="vlm_reviewed_visual_tree",
                    annotation_status="vlm_reviewed_unverified")
                result["postprocess"] = {"schema": "four_visual_levels_v1", "source_rule_version": tree.get("rule_version"),
                    "levels": LEVELS, "node_levels": levels, "operations": operations,
                    "level_roles_basis": "vlm_node_roles" if "levels" in review else "rule_contract_vlm_global_review",
                    "rejected_model_instructions": audit.get("rejected_operations", []),
                    "large_region_checks": checks, "large_region_decisions": review.get("large_region_decisions", []),
                    "review_model": config["api"]["model"], "training_ignore_required": True}
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(image, destination/image.name)
                write(destination/"hierarchy.json", result)
                audit.update(status="accepted", after=len(result["nodes"]))
    except Exception as error:
        # Avoid logging arbitrary transport exception strings containing URL credentials.
        audit["error"] = str(error) if isinstance(error, (ValueError, RuntimeError, KeyError)) else type(error).__name__
    audit["seconds"] = round(time.monotonic()-start, 3)
    write(destination/"layer-review.json", audit)
    return {k: audit[k] for k in ("source", "status", "seconds", "timings", "error", "before", "after", "rule_summary", "review_blockers") if k in audit}


