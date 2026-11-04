"""Compact model context; full annotation/provenance remains untouched on disk."""
from copy import deepcopy
from rules import validate

COLUMNS = ["parent", "layer", "class", "bbox"]
REASONS = {
    "rule_open_oversized_layer1_frontier": "open_region",
    "rule_bottom_up_visual_content_group": "content_group",
    "rule_layout_table_to_visual_regions": "layout_table",
    "rule_severely_clipped_boundary_fragment": "clipped_fragment",
    "rule_near_parent_size": "same_box",
    "rule_sibling_overlap": "duplicate",
    "rule_unary_wrapper_for_depth_budget": "wrapper",
    "rule_detail_beyond_four_levels": "fine_detail",
    "rule_page_wrapper_to_virtual_root": "page_wrapper",
}


def row(node):
    """Transport bbox to 0.1 CSS px precision only; saved dataset coordinates remain exact."""
    return [node["parent_id"], node["layer"], node["struct"], [round(v, 1) if i < 2 else max(.1, round(v, 1)) for i, v in enumerate(node["bbox"])]]


def original_boxes(tree, current=None):
    """Supply EVERY original visual box; obsolete DOM depths remain in the local source file."""
    active = {n["node_id"] for n in (current or tree)["nodes"]}
    return {"size": [tree["capture"]["width"], tree["capture"]["height"]], "box_columns": ["class", "bbox", "active_in_initial_candidate"],
        "boxes": {n["node_id"]: row(n)[2:]+[int(n["node_id"] in active)] for n in tree["nodes"]},
        "hints": hints(tree, active)}


def current_tree(original, current):
    """Transmit authoritative current edges, inheriting unchanged visual boxes from original IDs."""
    old = {n["node_id"]: n for n in original["nodes"]}
    children = {}
    for n in current["nodes"]:
        children.setdefault(n["parent_id"], []).append(n["node_id"])
    return {"roots": children.pop(None, []), "children": children,
        "inactive_original_ids": sorted(set(old)-{n["node_id"] for n in current["nodes"]}),
        "box_overrides": {n["node_id"]: row(n)[2:] for n in current["nodes"] if n["node_id"] not in old or row(n)[2:] != row(old[n["node_id"]])[2:]},
        "layers": "roots=L1; each children edge adds one level; leaves need not reach L4",
        "source_boxes": "original.boxes, with box_overrides applied",
        "hints": hints(current, {n["node_id"] for n in current["nodes"]})}


def reconstruct_current_boxes(original, state):
    """Test oracle: recover every current box/parent/layer from original visual facts and edges."""
    source = {n["node_id"]: row(n)[2:] for n in original["nodes"]}
    source.update(deepcopy(state["box_overrides"]))
    parents = {key: None for key in state["roots"]}
    for parent, ids in state["children"].items():
        for key in ids:
            if key in parents:
                raise ValueError("Current ID has multiple parent/root assignments")
            parents[key] = parent
    tree = {"capture": original["capture"], "nodes": [
        {"node_id": key, "parent_id": parent, "bbox": source[key][1]} for key, parent in parents.items()]}
    depths = validate(tree)
    return {key: [parent, depths[key], *source[key]] for key, parent in parents.items()}


def hints(tree, ids):
    """Keep structural hints needed for headings/regions/layout tables, without verbose DOM history."""
    roles = {"heading", "header", "footer", "main", "nav", "navigation", "aside", "section", "article", "table", "table_row", "layout_group", "figure"}
    return {n["node_id"]: n["subtype"] for n in tree["nodes"] if n["node_id"] in ids and n.get("subtype") in roles}


def summarize_changes(operations):
    """Coalesce consecutive unary changes into short IDs; never send long narrative reasons."""
    result = []
    for op in operations:
        if op.get("outcome") == "already_omitted":
            continue
        why = REASONS.get(op.get("reason"), "rule" if str(op.get("reason", "")).startswith("rule_") else "visual_edit")
        action = op.get("op")
        if action in ("contract", "drop"):
            if result and result[-1].get("op") == action and result[-1].get("why") == why:
                result[-1]["nodes"].append(op["node"])
            else:
                result.append({"op": action, "nodes": [op["node"]], "why": why})
        else:
            result.append({**{k: deepcopy(v) for k, v in op.items() if k in ("op", "node", "nodes", "parent", "keep", "id")}, "why": why})
    return result


def rejected_changes(rejected):
    """Expose failed edits and short validation codes, not duplicated provider explanations."""
    result = []
    for item in rejected:
        op = item.get("operation", {})
        error = item.get("error", "")
        code = "cycle" if "Cyclic" in error else "preserve_region" if "Preserve" in error else "invalid_reference" if "node" in error.lower() else "invalid_edit"
        result.append({"edit": {k: v for k, v in op.items() if k in ("op", "node", "nodes", "parent", "id", "keep")}, "code": code})
    return result


def build_delta(before, after, operations, sibling_limit=2):
    """Describe actual changes plus local family context; omit unchanged global subtrees.

    Parent/bbox/class changes are explicit. Layer-only changes are inherited from parent
    links and summarized by subtree, so the resulting tree can be reconstructed exactly
    up to the declared transport coordinate precision.
    """
    old = {n["node_id"]: n for n in before["nodes"]}
    new = {n["node_id"]: n for n in after["nodes"]}
    removed, added = set(old)-set(new), set(new)-set(old)
    structural = {key for key in old.keys() & new.keys() if any(old[key][field] != new[key][field] for field in ("parent_id", "struct", "bbox"))}
    touched = removed | added | structural
    for op in operations:
        for field in ("node", "parent", "keep", "id"):
            value = op.get(field)
            if isinstance(value, str) and (value in old or value in new):
                touched.add(value)
        touched.update(key for key in op.get("nodes", []) if key in old or key in new)
    scope = set(touched)
    for index in (old, new):
        siblings = {}
        for n in index.values():
            siblings.setdefault(n["parent_id"], []).append(n)
        for key in touched:
            if key not in index:
                continue
            node = index[key]
            parent = node["parent_id"]
            seen = set()
            while parent in index and parent not in seen:
                seen.add(parent)
                scope.add(parent)
                parent = index[parent]["parent_id"]
            x, y, w, h = node["bbox"]
            nearby = [n for n in siblings[node["parent_id"]] if n["node_id"] != key]
            nearby.sort(key=lambda n: ((n["bbox"][0]+n["bbox"][2]/2-x-w/2)**2+(n["bbox"][1]+n["bbox"][3]/2-y-h/2)**2, n["node_id"]))
            scope.update(n["node_id"] for n in nearby[:sibling_limit])
    shifts = []
    for key in old.keys() & new.keys():
        delta = new[key]["layer"]-old[key]["layer"]
        if not delta:
            continue
        parent = new[key]["parent_id"]
        if parent in old and parent in new and new[parent]["layer"]-old[parent]["layer"] == delta:
            continue
        shifts.append({"root": key, "by": delta})
    return {"box_columns": COLUMNS, "bbox_precision_px": .1,
        "changes": summarize_changes(operations), "removed": sorted(removed),
        "before": {key: row(n) for key, n in old.items() if key in scope},
        "after": {key: row(n) for key, n in new.items() if key in scope},
        "subtree_layer_shifts": sorted(shifts, key=lambda s: s["root"]),
        "level1_after": {key: row(n) for key, n in new.items() if n["parent_id"] is None},
        "hints_after": hints(after, scope),
        "counts": {"before": len(old), "after": len(new), "added": len(added), "removed": len(removed)},
        "context": {"nearby_siblings_per_changed_node": sibling_limit, "unlisted_nodes": "unchanged; derive layers from current parents"}}


def reconstruct_boxes(before, delta):
    """Local test oracle for patch completeness, not a model/API operation."""
    boxes = {n["node_id"]: row(n) for n in before["nodes"]}
    for key in delta["removed"]:
        boxes.pop(key, None)
    boxes.update(deepcopy(delta["after"]))
    for update in delta.get("parent_updates", []):
        for key in update["nodes"]:
            boxes[key][0] = update["parent"]
    tree = {"capture": before["capture"], "nodes": [
        {"node_id": key, "parent_id": value[0], "bbox": value[3]} for key, value in boxes.items()]}
    depths = validate(tree)
    for key in boxes:
        boxes[key][1] = depths[key]
    return boxes


def review_patch(before, after, operations):
    """Transmit local before/after topology; inherit visual boxes already present in history."""
    delta = build_delta(before, after, operations)
    old = {n["node_id"]: row(n) for n in before["nodes"]}
    visuals = {key: value[2:] for key, value in delta["after"].items() if key not in old or value[2:] != old[key][2:]}
    return {"topology_columns": ["parent", "layer"], "visual_columns": ["class", "bbox"],
        "before": {key: value[:2] for key, value in delta["before"].items()},
        "after": {key: value[:2] for key, value in delta["after"].items()},
        "visual_changes": visuals, "removed": delta["removed"], "changes": delta["changes"],
        "subtree_layer_shifts": delta["subtree_layer_shifts"],
        "level1_after": list(delta["level1_after"]), "counts": delta["counts"],
        "visual_reference": "Previous current_tree/patch visuals, inheriting original.boxes; unlisted visuals are unchanged",
        "hints_after": delta["hints_after"], "context": delta["context"]}


def reconstruct_review_boxes(before, patch):
    """Test oracle proving local topology/visual updates preserve the entire resulting tree."""
    boxes = {n["node_id"]: row(n) for n in before["nodes"]}
    for key in patch["removed"]:
        boxes.pop(key, None)
    for key, value in patch["after"].items():
        visual = patch["visual_changes"].get(key, boxes.get(key, [None, None, None, None])[2:])
        boxes[key] = value+deepcopy(visual)
    tree = {"capture": before["capture"], "nodes": [
        {"node_id": key, "parent_id": value[0], "bbox": value[3]} for key, value in boxes.items()]}
    depths = validate(tree)
    for key in boxes:
        boxes[key][1] = depths[key]
    return boxes
