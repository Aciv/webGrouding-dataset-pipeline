"""Pure saved-tree operations in screenshot CSS pixels; never requires live DOM."""
from collections import Counter
from copy import deepcopy
import math

LEVELS = {
    "1": "页面大区域：导航栏、侧栏、主体内容区、页脚等；不是整页包装框",
    "2": "大区域内的组件/分组：卡片、图文组、表格、表单、菜单组等",
    "3": "组件内主要内容：图片、文本、控件、表格行/单元格等；与第二级构成监督主力",
    "4": "必要的深层细节；低价值碎片可省略，分支可以提前结束",
}


def validate(tree, max_levels=None):
    """Check IDs, links, acyclicity and image bounds; derive depths from parent links."""
    nodes = tree["nodes"]
    index = {n["node_id"]: n for n in nodes}
    if len(index) != len(nodes):
        raise ValueError("Duplicate node IDs")
    width, height = tree["capture"]["width"], tree["capture"]["height"]
    if width <= 0 or height <= 0:
        raise ValueError("Invalid capture dimensions")
    depths = {}
    for node in nodes:
        box = node["bbox"]
        if len(box) != 4 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
            raise ValueError("Invalid bbox")
        x, y, w, h = box
        if x < -0.5 or y < -0.5 or w <= 0 or h <= 0 or x+w > width+0.5 or y+h > height+0.5:
            raise ValueError(f"BBox outside screenshot: {node['node_id']}")
        chain, current = [], node["node_id"]
        while current is not None and current not in depths:
            if current in chain:
                raise ValueError("Cyclic parent links")
            if current not in index:
                raise ValueError(f"Missing parent: {current}")
            chain.append(current)
            current = index[current]["parent_id"]
        depth = depths[current] if current is not None else -1
        for item in reversed(chain):
            depth += 1
            depths[item] = depth
    if max_levels is not None and any(d >= max_levels for d in depths.values()):
        raise ValueError(f"Tree exceeds {max_levels} semantic levels; review needed, no silent truncation")
    return depths


def overlap(a, b):
    """IoU for xywh boxes, independent of DOM/tag/category."""
    area = max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0])) * max(0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))
    return area / (a[2]*a[3]+b[2]*b[3]-area)


def union(boxes):
    """Bound approved group members with an outer rectangle; do not invent pixel geometry."""
    x, y = min(b[0] for b in boxes), min(b[1] for b in boxes)
    return [x, y, max(b[0]+b[2] for b in boxes)-x, max(b[1]+b[3] for b in boxes)-y]


def propose(tree, policy):
    """Propose geometry edits only; no proposal is applied before VLM approval."""
    depths = validate(tree)
    nodes = tree["nodes"]
    index = {n["node_id"]: n for n in nodes}
    counts = Counter(depths.values())
    children = {key: [] for key in index}
    for n in nodes:
        if n["parent_id"] is not None:
            children[n["parent_id"]].append(n["node_id"])
    result = []
    for n in nodes:
        parent = index.get(n["parent_id"])
        if parent and all(abs(n["bbox"][i]/parent["bbox"][i]-1) <= policy.get("parent_size_tolerance", .02)+1e-9 for i in (2, 3)):
            result.append({"op": "contract", "node": n["node_id"], "reason": "near_parent_size", "iou": overlap(n["bbox"], parent["bbox"])})
        if counts[depths[n["node_id"]]] == 1:
            result.append({"op": "review_singleton", "node": n["node_id"], "children": children[n["node_id"]], "reason": "compress_ancestors_or_drop_low_value_leaf"})
    groups = {}
    for n in nodes:
        groups.setdefault(n["parent_id"], []).append(n)
    for siblings in groups.values():
        for i, a in enumerate(siblings):
            for b in siblings[i+1:]:
                if overlap(a["bbox"], b["bbox"]) >= policy.get("sibling_iou", .9):
                    result.append({"op": "merge", "nodes": [a["node_id"], b["node_id"]], "reason": "sibling_overlap"})
        small = [n for n in siblings if n["struct"] in ("text", "graphic") and max(n["bbox"][2:]) <= policy.get("fragment_max_size", 36)]
        if len(small) >= 3:
            result.append({"op": "review_fragments", "nodes": [n["node_id"] for n in small], "reason": "review_independent_controls_vs_low_value_fragments"})
    return result


def apply_operations(tree, operations):
    """Apply an atomic VLM edit transaction; preserve source IDs and ignored foreground."""
    result = deepcopy(tree)
    nodes = {n["node_id"]: n for n in result["nodes"]}
    ignored = result.setdefault("ignore_regions", [])
    provenance = result.setdefault("postprocess_sources", {})
    decisions = {d["node_id"]: d for d in result.get("decisions", [])}
    trail = []
    def get(key):
        if key not in nodes:
            raise ValueError(f"Unknown/deleted node: {key}")
        return nodes[key]
    def remove(key, representative, reason, ignore=False):
        n = get(key)
        provenance[key] = deepcopy(n)
        for child in nodes.values():
            if child["parent_id"] == key:
                child["parent_id"] = representative
        if ignore:
            ignored.append({"node_id": key, "bbox": n["bbox"], "reason": "postprocess_"+reason})
        decisions[key] = {"node_id": key, "status": "excluded", "reason": "postprocess_"+reason, "represented_by": representative}
        del nodes[key]
    for op in operations:
        if not isinstance(op.get("reason"), str) or not op["reason"].strip():
            raise ValueError("Every edit requires a visual reason")
        action = op["op"]
        if action in ("contract", "drop"):
            if op["node"] not in nodes and op["node"] in provenance:
                trail.append({**deepcopy(op), "outcome": "already_omitted"})
                continue  # A provider may repeat an already-applied rule; do not spend another call.
            node = get(op["node"])
            if action == "drop" and any(n["parent_id"] == node["node_id"] for n in nodes.values()):
                raise ValueError("drop only accepts leaves; contract containers to preserve descendants")
            unrepresented_leaf = node["parent_id"] is None and not any(n["parent_id"] == node["node_id"] for n in nodes.values())
            remove(node["node_id"], node["parent_id"], op["reason"], action == "drop" or unrepresented_leaf)
        elif action == "reparent":
            node, parent = get(op["node"]), op["parent"]
            if parent is not None:
                get(parent)
            node["parent_id"] = parent
        elif action == "restore":
            key, parent = op["node"], op["parent"]
            if key in nodes or key not in provenance:
                raise ValueError("restore requires an omitted original node")
            if parent is not None:
                get(parent)
            restored = deepcopy(provenance[key])
            restored["parent_id"] = parent
            nodes[key] = restored
            ignored[:] = [region for region in ignored if region["node_id"] != key]
        elif action in ("merge", "group"):
            ids = op["nodes"]
            if len(ids) < 2 or len(set(ids)) != len(ids):
                raise ValueError("Group/merge requires distinct members")
            members = [get(key) for key in ids]
            if len({n["parent_id"] for n in members}) != 1:
                raise ValueError("Group/merge members must currently share a parent")
            bbox = union([n["bbox"] for n in members])
            if action == "merge":
                winner = get(op["keep"])
                if winner["node_id"] not in ids:
                    raise ValueError("Merge representative must be a member")
                winner["bbox"] = bbox
                winner["source_node_ids"] = list(dict.fromkeys(s for n in members for s in n.get("source_node_ids", [n["node_id"]])))
                for key in ids:
                    if key != winner["node_id"]:
                        remove(key, winner["node_id"], op["reason"])
            else:
                key = op["id"]
                if not isinstance(key, str) or not key.startswith("vlm_group_") or key in nodes or key in provenance:
                    raise ValueError("New unique group ID must start with vlm_group_")
                group = deepcopy(members[0])
                group.update(node_id=key, parent_id=members[0]["parent_id"], bbox=bbox, layout_bbox=bbox,
                    postprocess_generated=True,
                    struct="block", kind="group", subtype="visual_group", semantic_hint="visual_group",
                    disposition="detection", tag="", selector="", source_dom_parent_id=None,
                    interaction_role=None, semantic_role=None, text_role=None, fragments=[],
                    geometry_warnings=[], reasons=["rule_proposed_visual_group" if op["reason"].startswith("rule_") else "vlm_proposed_visual_group"],
                    source_node_ids=list(dict.fromkeys(s for n in members for s in n.get("source_node_ids", [n["node_id"]]))))
                nodes[key] = group
                for n in members:
                    n["parent_id"] = key
        else:
            raise ValueError(f"Unsupported operation: {action}")
        result["nodes"] = list(nodes.values())
        validate(result)
        trail.append(deepcopy(op))
    if not nodes and tree["nodes"]:
        raise ValueError("Empty result requires manual page review")
    depths = validate(result)
    # New visual groups describe their CURRENT members, including after reparent/drop.
    # Original DOM-backed boxes retain their recorded geometry.
    for key in sorted(nodes, key=lambda item: depths[item], reverse=True):
        n = nodes[key]
        members = [child for child in nodes.values() if child["parent_id"] == key]
        if n.get("postprocess_generated") and members:
            n["bbox"] = union([child["bbox"] for child in members])
            n["layout_bbox"] = list(n["bbox"])
            n["source_node_ids"] = list(dict.fromkeys(source for child in members for source in child.get("source_node_ids", [child["node_id"]])))
    for n in nodes.values():
        n["layer"] = depths[n["node_id"]]
        decisions[n["node_id"]] = {"node_id": n["node_id"], "status": "retained", "reason": "postprocess_retained", "represented_by": n["node_id"]}
    for d in decisions.values():
        seen = set()
        key = d.get("represented_by")
        while key is not None and key not in nodes:
            if key in seen:
                key = None
                break
            seen.add(key)
            key = decisions.get(key, {}).get("represented_by")
        d["represented_by"] = key
    result["decisions"] = list(decisions.values())
    stats = result.setdefault("stats", {})
    # Synthetic groups are not original DOM candidates; do not subtract them from raw counts.
    generated = sum(n.get("postprocess_generated") is True for n in nodes.values())
    stats.update(retained=len(nodes),
        excluded=max(0, stats.get("raw_visible", len(tree["nodes"]))-(len(nodes)-generated)),
        synthetic_groups=generated,
        roots=sum(n["parent_id"] is None for n in nodes.values()), max_layer=max(depths.values(), default=-1))
    for field in ("layer", "kind", "struct", "disposition"):
        stats["by_"+field] = dict(Counter(str(n[field]) for n in nodes.values()))
    return result, trail
