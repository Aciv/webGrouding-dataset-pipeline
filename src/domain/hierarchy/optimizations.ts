import type { Box, RawNode, RawSnapshot } from "../types.js";
import { MIN_VISIBLE_AREA, projectVisibleNodes } from "../visibility.js";
import { isControl, paintEvidence, type Candidate } from "./candidates.js";
import type { HierarchyDecision } from "./types.js";

/** Axis-aligned rendered-content union; never changes native DOM layout facts. */
function union(boxes: Box[]): Box {
  const x = Math.min(...boxes.map(b => b.x)), y = Math.min(...boxes.map(b => b.y));
  return { x, y, width: Math.max(...boxes.map(b => b.x + b.width)) - x,
    height: Math.max(...boxes.map(b => b.y + b.height)) - y };
}

/** Require complete containment for sibling-icon ownership; overlap alone is insufficient. */
function contains(outer: Box, inner: Box): boolean {
  return inner.x >= outer.x - 0.5 && inner.y >= outer.y - 0.5 &&
    inner.x + inner.width <= outer.x + outer.width + 0.5 &&
    inner.y + inner.height <= outer.y + outer.height + 0.5;
}

/** Apply conservative, traceable visual rules. Text-leaf and deepest-layer policies stay unchanged.
 * Geometry corrections are clipped again using the same screenshot/ancestor rules.
 */
export function optimizeCandidates(snapshot: RawSnapshot, candidates: Map<string, Candidate>,
    decisions: Map<string, HierarchyDecision>): void {
  const all = new Map(snapshot.nodes.map(node => [node.nodeId, node]));
  const children = new Map<string, RawNode[]>();
  for (const node of snapshot.nodes) if (node.parentId) {
    const list = children.get(node.parentId) ?? [];
    list.push(node); children.set(node.parentId, list);
  }
  /** Bound ancestry traversal to recorded DOM; no semantic parent is fabricated. */
  const ancestry = (node: RawNode): RawNode[] => {
    const result: RawNode[] = [], seen = new Set([node.nodeId]);
    let parent = node.parentId;
    while (parent && !seen.has(parent)) {
      seen.add(parent);
      const item = all.get(parent);
      if (!item) break;
      result.push(item); parent = item.parentId;
    }
    return result;
  };
  /** Exclusions retain a reason and optional visual owner for review and training ignore masks. */
  const exclude = (node: RawNode, reason: string, owner: string | null = null): void => {
    candidates.delete(node.nodeId);
    decisions.set(node.nodeId, { node_id: node.nodeId, status: "excluded", reason, represented_by: owner });
  };
  /** Reject a whole decorative/advertising subtree so its primitives cannot become new labels. */
  const excludeTree = (node: RawNode, reason: string): void => {
    for (const candidate of [...candidates.values()])
      if (candidate.node.nodeId === node.nodeId || ancestry(candidate.node).some(p => p.nodeId === node.nodeId))
        exclude(candidate.node, reason);
  };
  for (const candidate of [...candidates.values()]) {
    const node = candidate.node;
    if (!candidates.has(node.nodeId)) continue;
    const parents = ancestry(node);
    // Explicit ad semantics only; ordinary embedded maps, videos and editors remain candidates.
    if (node.tag === "iframe" && /\b(advertisement|adsbygoogle)\b/i.test(
        [node.attributes.title, node.attributes["aria-label"], ...parents.map(p => p.attributes.class)].join(" "))) {
      excludeTree(node, "ignore_advertising_embed");
      continue;
    }
    // Wide, shallow, non-interactive path-only SVGs are divider candidates, not all SVGs.
    const primitives = node.tag === "svg"
      ? snapshot.nodes.filter(n => ancestry(n).some(p => p.nodeId === node.nodeId)) : [];
    if (node.tag === "svg" && !node.interactiveHint && !parents.some(isControl) &&
        !node.attributes.role && !node.attributes["aria-label"] && !node.ownText &&
        [node, ...parents.slice(0, 2)].some(n => /wave|divider|separator/i.test(
          `${n.attributes.id || ""} ${n.attributes.class || ""}`)) &&
        node.bbox.width >= snapshot.viewport.width * 0.9 && node.bbox.height <= 180 &&
        node.bbox.width / node.bbox.height >= 6 && primitives.length > 0 &&
        primitives.every(n => /^(path|g|defs|clippath|lineargradient|stop)$/.test(n.tag))) {
      excludeTree(node, "ignore_decorative_divider_candidate");
      continue;
    }
    const direct = (children.get(node.nodeId) ?? []).filter(n => n.visibility.cssVisible && n.captureBbox);
    // Classification supplies explicit owned media; geometry does not independently re-guess its type.
    const mediaSource = [...candidate.structure.source_node_ids].reverse()
      .map(id => all.get(id)).find(item => item && item.nodeId !== node.nodeId &&
        /^(img|svg|canvas|video|audio)$/.test(item.tag));
    if (/^(a|picture)$/.test(node.tag) && mediaSource) {
      candidate.reasons.push(node.tag === "a" ? "media_link_content_bbox" : "picture_content_bbox");
      candidate.node = corrected(node, mediaSource.bbox, snapshot);
    }
    // Fix inline menu children escaping a UL/OL line box, without arbitrary deep unions.
    if (/^(ul|ol)$/.test(node.tag) && direct.length &&
        direct.every(n => n.tag === "li" && /^(inline|inline-block)$/.test(n.style.display || ""))) {
      const content = union(direct.map(n => n.bbox));
      candidate.node = corrected(node, union([node.bbox, content]), snapshot);
      candidate.reasons.push("inline_list_content_union");
    }
    const rect = candidate.node.captureBbox;
    if (!rect || rect.width * rect.height < MIN_VISIBLE_AREA) {
      exclude(node, rect ? "area_below_10" : "outside_ancestor_clip");
      continue;
    }
    // Narrow clipped text slices cannot supervise a complete word. Keep small intact icons.
    const ratio = rect.width * rect.height / (candidate.node.bbox.width * candidate.node.bbox.height);
    if ((node.ownText || candidate.hint === "text") && ratio < 0.3 &&
        Math.min(rect.width, rect.height) < 8) {
      exclude(node, "ignore_clipped_text_fragment");
      continue;
    }
    // Limit wrapper compression to transparent P around one control. No LI/STRONG leaf policy change.
    if (node.tag === "p" && !node.ownText && paintEvidence(node).length === 0 &&
        direct.length === 1 && isControl(direct[0]) && candidates.has(direct[0].nodeId))
      exclude(node, "transparent_single_control_wrapper", direct[0].nodeId);
  }
  // A non-actionable icon may be a sibling decoration of exactly one enclosing control.
  for (const candidate of [...candidates.values()]) {
    const icon = candidate.node;
    if (icon.tag !== "svg" || icon.attributes["aria-hidden"] !== "true" || icon.interactiveHint) continue;
    const parent = icon.parentId ? all.get(icon.parentId) : null;
    if (!parent || isControl(parent) || parent.ownText) continue;
    const owners = (children.get(parent.parentId ?? "") ?? []).filter(n =>
      isControl(n) && candidates.has(n.nodeId) && contains(n.bbox, icon.bbox));
    if (owners.length === 1) excludeTree(icon, "sibling_control_icon");
    if (owners.length === 1) {
      const decision = decisions.get(icon.nodeId)!;
      decision.represented_by = owners[0].nodeId;
    }
  }
}

/** Intersect corrected viewport-coordinate geometry with capture and ancestor scrollports. */
function corrected(node: RawNode, bbox: Box, snapshot: RawSnapshot): RawNode {
  const width = snapshot.capture?.width ?? snapshot.viewport.width;
  const height = snapshot.capture?.height ?? snapshot.viewport.height;
  const x = Math.max(0, bbox.x), y = Math.max(0, bbox.y);
  const clipped = { x, y, width: Math.max(0, Math.min(width, bbox.x + bbox.width) - x),
    height: Math.max(0, Math.min(height, bbox.y + bbox.height) - y) };
  const replacement = { ...node, bbox, captureBbox: clipped };
  const all = new Map(snapshot.nodes.map(n => [n.nodeId, n]));
  const chain: RawNode[] = [replacement];
  const seen = new Set([node.nodeId]);
  let parent = node.parentId;
  while (parent && !seen.has(parent)) {
    seen.add(parent);
    const item = all.get(parent);
    if (!item) break;
    chain.push(item); parent = item.parentId;
  }
  const projection = projectVisibleNodes({ ...snapshot, nodes: chain });
  return projection.nodes.find(n => n.nodeId === node.nodeId) ?? { ...replacement, captureBbox: null };
}
