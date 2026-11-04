import type { Box, RawNode, RawSnapshot } from "./types.js";
import { hiddenCssReason, type HiddenCssReason } from "./css-visibility.js";

/** Minimum retained screenshot-visible area in CSS px²; equality is retained. */
export const MIN_VISIBLE_AREA = 10;

/** CSS layout visibility alone does not account for independent ancestor scrollports. */
export interface ClippingResult {
  nodes: RawNode[];
  excluded: { node_id: string; reason: "outside_ancestor_clip" | "area_below_10" | HiddenCssReason }[];
  sourceVisible: number;
  partial: number;
}

/** Intersect only the axes clipped by this ancestor; reject empty rectangles. */
function intersection(a: Box, b: Box, clipX: boolean, clipY: boolean): Box | null {
  const x = clipX ? Math.max(a.x, b.x) : a.x, y = clipY ? Math.max(a.y, b.y) : a.y;
  const right = clipX ? Math.min(a.x + a.width, b.x + b.width) : a.x + a.width;
  const bottom = clipY ? Math.min(a.y + a.height, b.y + b.height) : a.y + a.height;
  return right > x && bottom > y ? { x, y, width: right - x, height: bottom - y } : null;
}

/** New captures measure the client box; old snapshots approximate it by removing borders. */
function scrollport(node: RawNode): Box {
  if (node.clientBbox) return node.clientBbox;
  const size = (value: string | undefined) => Math.max(0, parseFloat(value || "0") || 0);
  const left = size(node.style.borderLeft), right = size(node.style.borderRight);
  const top = size(node.style.borderTop), bottom = size(node.style.borderBottom);
  return { x: node.bbox.x + left, y: node.bbox.y + top,
    width: Math.max(0, node.bbox.width - left - right), height: Math.max(0, node.bbox.height - top - bottom) };
}

/** Apply nested, axis-aligned overflow clipping without rewriting source observations.
 * Shared by Node exporters and the browser. Arbitrary clip-path/occlusion is not inferred.
 */
export function projectVisibleNodes(snapshot: RawSnapshot): ClippingResult {
  const all = new Map(snapshot.nodes.map(node => [node.nodeId, node]));
  const nodes: RawNode[] = [], excluded: ClippingResult["excluded"] = [];
  let sourceVisible = 0, partial = 0;
  for (const node of snapshot.nodes) {
    if (!node.visibility.cssVisible || !node.captureBbox) continue;
    sourceVisible++;
    let hiddenReason = hiddenCssReason(node);
    let box: Box | null = { ...node.captureBbox };
    const visited = new Set([node.nodeId]);
    let parentId = node.parentId;
    while (box && parentId && !visited.has(parentId)) {
      visited.add(parentId);
      const parent = all.get(parentId);
      if (!parent) break;
      parentId = parent.parentId;
      hiddenReason ??= hiddenCssReason(parent);
      // Root scrolling defines the full-page image, unlike independent nested scrolling.
      if (/^(html|body)$/.test(parent.tag) || parent.style.display === "contents") continue;
      const clipX = /^(hidden|clip|auto|scroll|overlay)$/.test(parent.style.overflowX || "");
      const clipY = /^(hidden|clip|auto|scroll|overlay)$/.test(parent.style.overflowY || "");
      if (clipX || clipY) box = intersection(box, scrollport(parent), clipX, clipY);
    }
    if (hiddenReason) {
      excluded.push({ node_id: node.nodeId, reason: hiddenReason });
      continue;
    }
    if (!box) { excluded.push({ node_id: node.nodeId, reason: "outside_ancestor_clip" }); continue; }
    const original = node.captureBbox;
    if (box.x !== original.x || box.y !== original.y || box.width !== original.width || box.height !== original.height) partial++;
    // Strictly below 10 CSS px²; a 10 px² box remains eligible. Raw evidence stays intact.
    if (box.width * box.height < MIN_VISIBLE_AREA) {
      excluded.push({ node_id: node.nodeId, reason: "area_below_10" });
      continue;
    }
    nodes.push({ ...node, captureBbox: box });
  }
  return { nodes, excluded, sourceVisible, partial };
}
