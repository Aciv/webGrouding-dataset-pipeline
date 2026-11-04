import assert from "node:assert/strict";
import type { RawNode, RawSnapshot } from "../domain/types.js";
import { buildVisualHierarchy } from "../domain/hierarchy/normalize.js";
import { CHILD_GEOMETRY_POLICY } from "../domain/hierarchy/child-selection.js";
import { refineSavedHierarchy } from "../domain/hierarchy/refine-saved.js";

/** Source-like rectangles isolate child generation from browser rendering. */
function node(id: string, tag: string, parent: string | null,
    x: number, y: number, width: number, height: number): RawNode {
  const rect = { x, y, width, height };
  return { nodeId: id, parentId: parent, tag, selector: `#${id}`, attributes: {},
    ownText: tag === "p" ? id : "", textTruncated: false, bbox: rect,
    documentBbox: rect, captureBbox: rect, fragments: [rect], textRects: [], textRectsTruncated: false,
    style: { display: "block" }, interactiveHint: tag === "button",
    visibility: { cssVisible: true, intersectsViewport: true, clippingAncestors: [], occlusion: "not_measured" },
    pseudo: { before: "none", after: "none" } };
}

const nodes = [
  node("root", "main", null, 0, 0, 1000, 1000),
  node("parent", "section", "root", 10, 10, 200, 200),
  node("size-child", "section", "parent", 10, 10, 196, 204),
  node("grandchild", "button", "size-child", 20, 20, 40, 20),
  node("different-height", "section", "parent", 10, 10, 200, 194),
  node("siblings", "section", "root", 300, 10, 400, 300),
  node("a", "section", "siblings", 310, 20, 100, 100),
  node("b", "section", "siblings", 312, 20, 100, 100),
  node("a-text", "p", "a", 320, 30, 20, 10),
  node("b-text", "p", "b", 360, 30, 20, 10),
  node("low-overlap", "section", "siblings", 370, 20, 100, 100),
  node("left", "section", "root", 10, 400, 200, 200),
  node("right", "section", "root", 600, 400, 200, 200),
  node("left-control", "button", "left", 20, 410, 40, 20),
  node("right-control", "button", "right", 20, 410, 40, 20),
  node("union-parent", "section", "root", 10, 650, 100, 100),
  node("union-a", "section", "union-parent", 10, 650, 97, 100),
  node("union-b", "section", "union-parent", 13, 650, 97, 100),
  node("union-control", "button", "union-a", 20, 660, 20, 10),
];
const snapshot: RawSnapshot = { schema: "raw_dom_geometry_v1", documentId: "geometry", url: "https://fixture.test/",
  title: "Geometry", capturedAt: "2026-11-03T00:00:00Z", viewport: { width: 1000, height: 1000, dpr: 1 },
  scroll: { x: 0, y: 0 }, document: { width: 1000, height: 1000 }, coordinateSpace: "viewport_css_pixels",
  scope: "main_frame_light_dom", totalNodes: nodes.length, truncated: false, nodes };
const before = JSON.stringify(snapshot);
const result = buildVisualHierarchy(snapshot);
const map = new Map(result.nodes.map(item => [item.node_id, item]));
assert(!map.has("size-child"));
assert(map.has("different-height"), "3% height difference must not trigger the 2% rule");
assert.equal(map.get("grandchild")?.parent_id, "parent");
assert(map.has("a") && !map.has("b"));
assert.deepEqual(map.get("a")?.bbox, [310, 20, 102, 100]);
assert(map.get("a")?.source_node_ids.includes("b"));
assert.equal(map.get("b-text")?.parent_id, "a");
assert.equal(map.get("a-text")?.parent_id, "a");
assert(map.has("low-overlap"));
assert(map.has("left-control") && map.has("right-control"), "Cousins must not merge across parent groups");
assert(!map.has("union-a") && !map.has("union-b"), "Recheck parent size after sibling union");
assert.equal(map.get("union-control")?.parent_id, "union-parent");
assert.equal(result.decisions.find(item => item.node_id === "b")?.represented_by, "a");
for (const item of result.nodes)
  assert.equal(item.layer, item.parent_id ? map.get(item.parent_id)!.layer + 1 : 0);
assert.deepEqual(result.geometry_policy, CHILD_GEOMETRY_POLICY);
assert.equal(JSON.stringify(snapshot), before);
console.log("PASS: child-time size rejection, sibling union/ownership, promotion before expansion, cousin isolation and immutable raw DOM");

const saved = structuredClone(result);
const savedParent = saved.nodes.find(item => item.node_id === "parent")!;
saved.nodes.push({ ...savedParent, node_id: "old-wrapper", parent_id: "parent", layer: savedParent.layer + 1,
  source_node_ids: ["old-wrapper"] });
saved.decisions.push({ node_id: "old-wrapper", status: "retained", reason: "old_wrapper", represented_by: "old-wrapper" });
saved.nodes.find(item => item.node_id === "grandchild")!.parent_id = "old-wrapper";
const serialized = JSON.stringify(saved);
const refined = refineSavedHierarchy(saved);
assert(!refined.nodes.some(item => item.node_id === "old-wrapper"));
assert.equal(refined.nodes.find(item => item.node_id === "grandchild")?.parent_id, "parent");
assert.deepEqual(refineSavedHierarchy(refined).nodes, refined.nodes);
assert.equal(JSON.stringify(saved), serialized);
console.log("PASS: exported-tree offline refinement, parent regeneration, idempotence and source label preservation");
