"""Deterministic coarse four-level candidate construction, subject to later visual review."""
from collections import Counter
from rules import apply_operations, overlap, validate
from layout import layout_operations
from semantic_cleanup import generic_wrapper
from frontier import bottom_up_operations, clipped_fragment_operations, large_region_requirements


def normalize(tree, policy):
    """Compress wrappers before limiting detail; preserve removed boxes/provenance for review.

    This is a candidate tree, not verified semantic truth. No max-depth failure is caused
    by a valid input tree: unavoidable over-depth details become explicit ignore regions.
    """
    result, _ = apply_operations(tree, [])
    trail = []
    layout_done = False
    layout_evidence = []
    def apply(op):
        nonlocal result
        result, _ = apply_operations(result, [op])
        trail.append(op)

    while True:
        nodes = {n["node_id"]: n for n in result["nodes"]}
        children = {key: [] for key in nodes}
        for n in nodes.values():
            if n["parent_id"] is not None:
                children[n["parent_id"]].append(n)
        depths = validate(result)
        tolerance = policy.get("parent_size_tolerance", .02)
        fragments = clipped_fragment_operations(result, policy)
        operation = fragments[0] if fragments else None
        # Same-size redundant child identity; keep its descendants through contraction.
        for n in nodes.values():
            if operation is not None:
                break
            parent = nodes.get(n["parent_id"])
            if parent and all(abs(n["bbox"][i]/parent["bbox"][i]-1) <= tolerance+1e-9 for i in (2, 3)):
                operation = {"op": "contract", "node": n["node_id"], "reason": "rule_near_parent_size"}
                break
        # Remove page-sized generic wrappers from detection levels, not full-page media/text.
        if operation is None:
            canvas = [0, 0, result["capture"]["width"], result["capture"]["height"]]
            for n in nodes.values():
                if n["parent_id"] is None and n["struct"] == "block" and children[n["node_id"]] and overlap(n["bbox"], canvas) >= .95:
                    operation = {"op": "contract", "node": n["node_id"], "reason": "rule_page_wrapper_to_virtual_root"}
                    break
        # Merge only true siblings; stable order and winner priority make output reproducible.
        if operation is None:
            siblings = {}
            for n in nodes.values():
                siblings.setdefault(n["parent_id"], []).append(n)
            for group in siblings.values():
                ordered = sorted(group, key=lambda n: (-n.get("selection_priority", 0), n["node_id"]))
                for i, a in enumerate(ordered):
                    for b in ordered[i+1:]:
                        if overlap(a["bbox"], b["bbox"]) >= policy.get("sibling_iou", .9):
                            operation = {"op": "merge", "nodes": [a["node_id"], b["node_id"]], "keep": a["node_id"], "reason": "rule_sibling_overlap"}
                            break
                    if operation:
                        break
                if operation:
                    break
        # Resolve coarse granularity before spending depth budget or dropping details.
        if operation is None:
            edits, evidence = bottom_up_operations(result, policy)
            if edits:
                layout_evidence.extend(evidence)
                for edit in edits:
                    apply(edit)
                continue
        if operation is None and not layout_done:
            edits, evidence = layout_operations(result, policy)
            layout_done = True
            if edits:
                layout_evidence.extend(evidence)
                for edit in edits:
                    apply(edit)
                continue
        deep = [n for n in nodes.values() if depths[n["node_id"]] >= 4]
        if operation is None and deep:
            # Preserve deep targets by contracting unary generic ancestors before omitting detail.
            ancestors = set()
            for n in deep:
                key = n["parent_id"]
                while key is not None:
                    ancestors.add(key)
                    key = nodes[key]["parent_id"]
            candidates = [nodes[key] for key in ancestors if nodes[key]["struct"] == "block" and len(children[key]) == 1 and
                (not policy.get("preserve_meaningful_deep", False) or generic_wrapper(nodes[key]))]
            if candidates:
                chosen = sorted(candidates, key=lambda n: (-depths[n["node_id"]], n["node_id"]))[0]
                operation = {"op": "contract", "node": chosen["node_id"], "reason": "rule_unary_wrapper_for_depth_budget"}
            elif policy.get("preserve_meaningful_deep", False):
                # New semantic pipeline preserves unresolved primary content for review.
                # Top-down cleanup has already omitted only explicitly low-value tiny leaves.
                break
            else:
                # Four-stage annotation intentionally omits details represented by a retained ancestor.
                # Remove deepest leaves first, never delete a branch together with useful descendants.
                chosen = sorted(deep, key=lambda n: (-depths[n["node_id"]], n["node_id"]))[0]
                operation = {"op": "drop", "node": chosen["node_id"], "reason": "rule_detail_beyond_four_levels"}
        if operation is None:
            break
        apply(operation)
    validate(result, None if policy.get("preserve_meaningful_deep", False) else 4)
    summary = {"before": len(tree["nodes"]), "after": len(result["nodes"]),
        "before_levels": max(validate(tree).values(), default=-1)+1,
        "after_levels": max(validate(result).values(), default=-1)+1,
        "layout_regions": layout_evidence,
        "large_region_checks": large_region_requirements(result, policy),
        "operations": dict(Counter(op["reason"] for op in trail))}
    return result, trail, summary
