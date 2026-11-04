"""Rebuild coarse visual regions from saved children, without revisiting DOM or pixels."""
from rules import union


def partition(nodes, policy, depth=0):
    """Split a child frontier along empty projection bands, never through a recorded box.

    This borrows recursive spatial division, not DCGen's pixel separator detector.
    Two division steps allow sidebar/content followed by intro/table regions without
    promoting every paragraph to a top-level section.
    """
    if len(nodes) < 2 or depth >= policy.get("layout_split_depth", 2):
        return [nodes]
    bounds = union([n["bbox"] for n in nodes])
    choices = []
    for axis in (0, 1):
        ordered = sorted(nodes, key=lambda n: (n["bbox"][axis], n["node_id"]))
        edge = ordered[0]["bbox"][axis]+ordered[0]["bbox"][axis+2]
        for pos in range(1, len(ordered)):
            gap = ordered[pos]["bbox"][axis]-edge
            threshold = max(policy.get("layout_min_gap", 24), min(bounds[2:])*policy.get("layout_gap_ratio", .025))
            if gap >= threshold:
                left, right = ordered[:pos], ordered[pos:]
                # Avoid splitting off a tiny star/caption as a new page region.
                a, b = union([n["bbox"] for n in left]), union([n["bbox"] for n in right])
                min_extent = min(a[axis+2], b[axis+2])/max(1, bounds[axis+2])
                if min_extent >= policy.get("layout_min_extent_ratio", .06):
                    choices.append((gap/max(1, bounds[axis+2]), -axis, pos, ordered))
            edge = max(edge, ordered[pos]["bbox"][axis]+ordered[pos]["bbox"][axis+2])
    if not choices:
        return [nodes]
    _, _, pos, ordered = max(choices, key=lambda item: item[:3])
    return partition(ordered[:pos], policy, depth+1)+partition(ordered[pos:], policy, depth+1)


def layout_operations(tree, policy):
    """Promote existing sections or replace mixed large shells with grouped child regions.

    The resulting roots are candidate training regions; original labels and source IDs
    remain recoverable. Full-page images/text/tables and small ordinary cards are exempt.
    """
    if policy.get("layout_enabled", True) is not True:
        return [], []
    children = {}
    nodes = {n["node_id"]: n for n in tree["nodes"]}
    for n in nodes.values():
        children.setdefault(n["parent_id"], []).append(n)
    operations, evidence = [], []
    for root in children.get(None, []):
        if root["struct"] not in ("block", "region") or root.get("postprocess_generated"):
            continue
        kids = children.get(root["node_id"], [])
        if len(kids) < 2 or root["bbox"][3] < tree["capture"]["height"]*.3:
            continue
        role = root.get("subtype", "")
        if role in ("header", "footer", "navigation", "aside"):
            continue
        structured = [n for n in kids if n["struct"] in ("block", "region", "table", "list")]
        sections = [n for n in kids if n.get("subtype") in ("section", "article") or n["struct"] == "region"]
        if len(sections) >= 2 and len(structured) == len(kids):
            operations.append({"op": "contract", "node": root["node_id"], "reason": "rule_promote_existing_visual_sections"})
            evidence.append({"root": root["node_id"], "action": "promote", "regions": [n["node_id"] for n in kids]})
            continue
        groups = partition(kids, policy)
        if len(groups) < 2 or all(len(g) == 1 for g in groups):
            continue  # No reliable regrouping evidence; do not blindly promote raw atoms.
        new_groups = []
        used = set(nodes) | set(tree.get("postprocess_sources", {}))
        for pos, group in enumerate(groups):
            ids = [n["node_id"] for n in group]
            if len(group) == 1:
                new_groups.append({"members": ids, "region": ids[0], "bbox": group[0]["bbox"]})
                continue
            key = f"vlm_group_layout_{root['node_id']}_{pos}"
            while key in used:
                key += "_new"
            used.add(key)
            operations.append({"op": "group", "nodes": ids, "id": key, "reason": "rule_group_child_frontier_by_empty_bands"})
            new_groups.append({"members": ids, "region": key, "bbox": union([n["bbox"] for n in group])})
        operations.append({"op": "contract", "node": root["node_id"], "reason": "rule_replace_mixed_layout_shell"})
        evidence.append({"root": root["node_id"], "action": "regroup_then_promote", "regions": new_groups})
    return operations, evidence
