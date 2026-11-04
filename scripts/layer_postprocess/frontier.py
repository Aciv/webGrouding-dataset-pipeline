"""Choose mixed-depth visual frontiers, then assemble groups before assigning four layers."""
from rules import union, validate

ATOMIC = {"image", "graphic", "media", "control"}


def area(box):
    """Area in screenshot CSS pixels, independent of page aspect ratio."""
    return box[2]*box[3]


def is_layout_table(node, children, index):
    """Propose table-as-layout only from heterogeneous rows; regular data tables remain intact."""
    rows = children.get(node["node_id"], [])
    if node.get("struct") != "table" or len(rows) < 3 or not all(n.get("subtype") == "table_row" for n in rows):
        return False
    heights = [n["bbox"][3] for n in rows]
    if max(heights)/max(1, min(heights)) < 3:
        return False
    pending, seen, classes = [n["node_id"] for n in rows], set(), set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        classes.add(index[key]["struct"])
        pending.extend(n["node_id"] for n in children.get(key, []))
    return "image" in classes and "control" in classes


def large_region_requirements(tree, policy):
    """Check ONLY final layer-1 roots against whole-image area, with explicit atomic exemptions."""
    threshold = policy.get("layer1_max_area_ratio", .6)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold < 1:
        raise ValueError("layer1_max_area_ratio must be between 0 and 1")
    index = {n["node_id"]: n for n in tree["nodes"]}
    children = {}
    for n in index.values():
        children.setdefault(n["parent_id"], []).append(n)
    canvas = tree["capture"]["width"]*tree["capture"]["height"]
    checks = []
    for n in children.get(None, []):
        ratio = area(n["bbox"])/canvas
        if ratio <= threshold:
            continue
        atomic = n["struct"] in ATOMIC
        checks.append({"node_id": n["node_id"], "area_ratio": round(ratio, 6),
            "threshold": threshold, "status": "exempt_atomic" if atomic else "requires_visual_decision",
            "basis": "complete_recorded_atomic_object" if atomic else "oversized_final_layer1",
            "layout_table_candidate": is_layout_table(n, children, index),
            "direct_children": len(children.get(n["node_id"], []))})
    return checks


def validate_large_region_review(tree, policy, review):
    """A bare accepted=true cannot bypass unresolved oversized first-level containers."""
    checks = large_region_requirements(tree, policy)
    required = {c["node_id"]: c for c in checks if c["status"] == "requires_visual_decision"}
    decisions = review.get("large_region_decisions", [])
    if not isinstance(decisions, list):
        return False, checks, ["large_region_decisions must be an array"]
    mapping, errors = {}, []
    for d in decisions:
        if not isinstance(d, dict) or not isinstance(d.get("node_id"), str) or d["node_id"] in mapping:
            errors.append("Invalid or duplicate large region decision")
            continue
        mapping[d["node_id"]] = d
    for key, check in required.items():
        d = mapping.get(key, {})
        if d.get("decision") != "keep_single_component" or not isinstance(d.get("reason"), str) or not d["reason"].strip():
            errors.append(f"{key}: oversized layer1 needs decomposition or explicit visual exemption")
        elif check["layout_table_candidate"]:
            errors.append(f"{key}: heterogeneous layout table must be decomposed, not exempted as data table")
    return not errors, checks, errors


def clipped_fragment_operations(tree, policy):
    """Discard severe boundary slivers using saved layout_bbox, preserving useful partial objects."""
    if policy.get("clipped_fragment_enabled", True) is not True:
        return []
    parents = {n["parent_id"] for n in tree["nodes"]}
    w, h = tree["capture"]["width"], tree["capture"]["height"]
    operations = []
    for n in tree["nodes"]:
        raw, box = n.get("layout_bbox"), n["bbox"]
        if not raw or raw[2] <= 0 or raw[3] <= 0 or n["node_id"] in parents:
            continue
        x, y, bw, bh = box
        boundary = x <= .5 or y <= .5 or x+bw >= w-.5 or y+bh >= h-.5
        original_outside = raw[0] < -.5 or raw[1] < -.5 or raw[0]+raw[2] > w+.5 or raw[1]+raw[3] > h+.5
        if boundary and original_outside and min(bw, bh) <= policy.get("clipped_strip_max_px", 8) and area(box)/area(raw) < policy.get("clipped_min_visible_ratio", .1):
            operations.append({"op": "drop", "node": n["node_id"], "reason": "rule_severely_clipped_boundary_fragment"})
    return operations


def bottom_up_operations(tree, policy):
    """Search deeper frontiers beneath oversized L1 wrappers, then group headings with content.

    Frontier members form an antichain: never select both an ancestor and its child.
    Depth limits are enforced only AFTER this reconstruction. Different branches may
    stop at different original depths; retained atoms are not globally clustered.
    """
    if policy.get("layout_enabled", True) is not True:
        return [], []
    index = {n["node_id"]: n for n in tree["nodes"]}
    children = {}
    for n in index.values():
        children.setdefault(n["parent_id"], []).append(n)
    canvas = tree["capture"]["width"]*tree["capture"]["height"]
    threshold = policy.get("layer1_max_area_ratio", .6)
    depths = validate(tree)
    operations, evidence = [], []
    used = set(index) | set(tree.get("postprocess_sources", {}))
    for check in large_region_requirements(tree, policy):
        root = index[check["node_id"]]
        if check["status"] == "exempt_atomic" or root.get("postprocess_generated"):
            continue
        if not children.get(root["node_id"]):
            continue
        if root["struct"] == "table" and not check["layout_table_candidate"]:
            continue
        frontier, opened = [], []
        def descend(node):
            kids = children.get(node["node_id"], [])
            oversized = area(node["bbox"])/canvas > threshold
            table = node["struct"] == "table"
            expandable = node["struct"] in ("block", "region") or table and is_layout_table(node, children, index)
            if oversized and expandable and kids and not node.get("postprocess_generated"):
                opened.append(node["node_id"])
                for child in kids:
                    descend(child)
            else:
                frontier.append(node)
        descend(root)
        if not opened or len(frontier) < 2:
            continue
        headings = [n for n in frontier if (n.get("subtype") == "heading" or n.get("text_role") == "heading") and n["bbox"][2] >= root["bbox"][2]*.3]
        headings.sort(key=lambda n: (n["bbox"][1], n["node_id"]))
        groups = {n["node_id"]: [n] for n in headings}
        assigned = {n["node_id"] for n in headings}
        for n in frontier:
            if n["node_id"] in assigned:
                continue
            eligible = []
            for pos, title in enumerate(headings):
                if title["parent_id"] != n["parent_id"]:
                    continue
                tb, nb = title["bbox"], n["bbox"]
                following = [v["bbox"][1] for v in headings[pos+1:] if v["parent_id"] == title["parent_id"]]
                end = min(following) if following else root["bbox"][1]+root["bbox"][3]+1
                horizontal = max(0, min(tb[0]+tb[2], nb[0]+nb[2])-max(tb[0], nb[0]))/min(tb[2], nb[2])
                if nb[1] >= tb[1] and nb[1]+nb[3] <= end+1 and horizontal >= .5:
                    eligible.append(title)
            if eligible:
                title = max(eligible, key=lambda n: n["bbox"][1])
                groups[title["node_id"]].append(n)
                assigned.add(n["node_id"])
        grouped = [g for g in groups.values() if len(g) > 1]
        assigned = {n["node_id"] for group in grouped for n in group}
        remainder = [n for n in frontier if n["node_id"] not in assigned]
        # When only raw atoms remain, spatial whitespace may propose their larger groups.
        structured = [n for n in remainder if n["struct"] in ("block", "region", "table", "list")]
        if len(structured) < 2 and len(remainder) > 1:
            from layout import partition
            spatial = partition(remainder, policy)
            if len(spatial) > 1:
                grouped.extend(g for g in spatial if len(g) > 1)
                used_members = {n["node_id"] for group in grouped for n in group}
                remainder = [n for n in remainder if n["node_id"] not in used_members]
        components = sum(n["struct"] in ("block", "region", "table", "list") for n in remainder)+len(grouped)
        if components < 2 and not check["layout_table_candidate"]:
            continue  # No useful decomposition: keep unresolved instead of scattering atoms into L1.
        for key in sorted(opened, key=lambda key: (depths[key], key)):
            operations.append({"op": "contract", "node": key,
                "reason": "rule_layout_table_to_visual_regions" if key == root["node_id"] and check["layout_table_candidate"] else "rule_open_oversized_layer1_frontier"})
        regions = [{"region": n["node_id"], "members": [n["node_id"]], "source_layer": depths[n["node_id"]]+1} for n in remainder]
        for pos, group in enumerate(grouped):
            key = f"vlm_group_frontier_{root['node_id']}_{pos}"
            while key in used:
                key += "_new"
            used.add(key)
            members = [n["node_id"] for n in group]
            operations.append({"op": "group", "nodes": members, "id": key, "reason": "rule_bottom_up_visual_content_group"})
            regions.append({"region": key, "members": members, "bbox": union([n["bbox"] for n in group]),
                "source_layers": sorted({depths[n["node_id"]]+1 for n in group})})
        evidence.append({"root": root["node_id"], "action": "bottom_up_frontier", "area_ratio": check["area_ratio"],
            "opened_wrappers": opened, "regions": regions})
    return operations, evidence
