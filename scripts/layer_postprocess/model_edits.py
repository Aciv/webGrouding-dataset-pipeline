"""Validate model suggestions independently; only a final visual check can approve the result."""
from copy import deepcopy
import re
from rules import apply_operations


def apply_model_edits(tree, operations, policy=None):
    """Apply valid sequential edits, record rejected instructions and canonicalize new group IDs.

    A malformed/cyclic instruction never corrupts the saved tree. Rejections must be shown
    to the subsequent VLM verifier and retained in audit; they are not treated as successful edits.
    """
    result = deepcopy(tree)
    policy = policy or {}
    aliases, applied, rejected = {}, [], []
    for position, original in enumerate(operations):
        try:
            if not isinstance(original, dict):
                raise ValueError("Operation must be an object")
            op = deepcopy(original)
            if op.get("op") == "contract":
                current = next((n for n in result["nodes"] if n["node_id"] == op.get("node")), None)
                if current and current["parent_id"] is None and current.get("subtype") == "table_row":
                    children = [n for n in result["nodes"] if n["parent_id"] == current["node_id"]]
                    box = current["bbox"]
                    ratio = box[2]*box[3]/(result["capture"]["width"]*result["capture"]["height"])
                    if len(children) > 1 and ratio <= policy.get("layer1_max_area_ratio", .6):
                        raise ValueError("Preserve decomposed L1 layout row; contracting it would scatter cells/decorations into L1. Regroup inside the region instead.")
            if op.get("op") == "group" and isinstance(op.get("id"), str) and not op["id"].startswith("vlm_group_"):
                key = op["id"]
                used = {n["node_id"] for n in result["nodes"]} | set(result.get("postprocess_sources", {}))
                if key in used:
                    raise ValueError("New group ID collides with an existing source identity")
                canonical = "vlm_group_"+re.sub(r"[^a-zA-Z0-9_-]", "_", key)
                while canonical in used:
                    canonical += "_new"
                aliases[key] = canonical
                op["id"] = canonical
            for field in ("node", "parent", "keep", "id"):
                if isinstance(op.get(field), str):
                    op[field] = aliases.get(op[field], op[field])
            if isinstance(op.get("nodes"), list):
                op["nodes"] = [aliases.get(key, key) for key in op["nodes"]]
            updated, trail = apply_operations(result, [op])
            result = updated
            applied.extend(trail)
        except (ValueError, KeyError, TypeError) as error:
            rejected.append({"index": position, "operation": original, "error": str(error)})
    return result, applied, rejected
