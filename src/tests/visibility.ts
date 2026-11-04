import assert from "node:assert/strict";
import type { RawNode, RawSnapshot } from "../domain/types.js";
import { projectVisibleNodes } from "../domain/visibility.js";
import { buildVisualHierarchy } from "../domain/hierarchy/normalize.js";

/** Rectangles give independently calculable clipping expectations. */
function node(id: string, parent: string | null, y: number, height: number, extra: Partial<RawNode> = {}): RawNode {
  const rect = { x: 100, y, width: 100, height };
  return { nodeId: id, parentId: parent, tag: "div", selector: `#${id}`, attributes: {}, ownText: id,
    textTruncated: false, bbox: rect, documentBbox: rect, captureBbox: rect, fragments: [rect],
    textRects: [], textRectsTruncated: false, style: { display: "block" }, interactiveHint: false,
    visibility: { cssVisible: true, intersectsViewport: true, clippingAncestors: [], occlusion: "not_measured" },
    pseudo: { before: "none", after: "none" }, ...extra };
}
const nodes = [
  node("body", null, 0, 2000, { tag: "body", ownText: "", style: { overflowY: "auto" } }),
  node("scroll", "body", 100, 100, { ownText: "", style: { display: "block", overflowY: "auto", borderTop: "2px solid black", borderBottom: "2px solid black" } }),
  node("inside", "scroll", 110, 20),
  node("partial", "scroll", 190, 20),
  node("outside", "scroll", 206, 20),
  node("below-fold", "body", 1200, 30),
];
const snapshot: RawSnapshot = { schema: "raw_dom_geometry_v1", documentId: "clip-fixture", capturedAt: "2026-10-28T00:00:00Z",
  url: "http://example.test/", title: "Clipping", viewport: { width: 900, height: 600, dpr: 1 }, scroll: { x: 0, y: 0 },
  document: { width: 900, height: 2000 }, coordinateSpace: "viewport_css_pixels", scope: "main_frame_light_dom",
  totalNodes: nodes.length, truncated: false, nodes };
const source = JSON.stringify(snapshot);
const result = projectVisibleNodes(snapshot);
const visible = new Map(result.nodes.map(node => [node.nodeId, node]));
assert.equal(visible.has("outside"), false);
assert.deepEqual(visible.get("inside")!.captureBbox, { x: 100, y: 110, width: 100, height: 20 });
assert.deepEqual(visible.get("partial")!.captureBbox, { x: 100, y: 190, width: 100, height: 8 });
assert(visible.has("below-fold"), "Root scrolling must not discard full-page content");
const hierarchy = buildVisualHierarchy(snapshot);
assert(!hierarchy.nodes.some(node => node.node_id === "outside"));
assert.equal(hierarchy.decisions.find(node => node.node_id === "outside")?.reason, "outside_ancestor_clip");
assert.equal(JSON.stringify(snapshot), source, "Clipping must not rewrite raw evidence");

// New captures use measured client bounds, accounting for scrollbars and borders.
nodes[1].clientBbox = { x: 100, y: 105, width: 85, height: 90 };
nodes[1].style.overflowX = "auto";
const measured = new Map(projectVisibleNodes(snapshot).nodes.map(node => [node.nodeId, node]));
assert.deepEqual(measured.get("partial")!.captureBbox, { x: 100, y: 190, width: 85, height: 5 });
console.log("PASS: nested scroll clipping, partial bounds, full-page root scrolling, measured client boxes and source preservation");

const cssNodes = [
  node("css-root", null, 0, 1000, { tag: "body" }),
  node("filter-hidden", "css-root", 20, 40, { style: { filter: "blur(1px) opacity(0%)" } }),
  node("filter-child", "filter-hidden", 25, 20),
  node("clip-hidden", "css-root", 60, 30,
    { style: { position: "absolute", clip: "rect(0px, 100px, 0px, 0px)" } }),
  node("clip-child", "clip-hidden", 65, 20),
  node("static-clip", "css-root", 100, 30, { style: { position: "static", clip: "rect(0px, 0px, 0px, 0px)" } }),
  node("inset-hidden", "css-root", 140, 30, { style: { clipPath: "inset(50%)" } }),
  node("partial-inset", "css-root", 180, 30, { style: { clipPath: "inset(10%)" } }),
  node("unknown-mask", "css-root", 220, 30, { style: { clipPath: "url(#mask)" } }),
  node("polygon-hidden", "css-root", 260, 30, { style: { clipPath: "polygon(0 0, 0 0, 0 0)" } }),
  node("circle-hidden", "css-root", 300, 30, { style: { clipPath: "circle(0px at center)" } }),
  node("sr-only", "css-root", 340, 100, {
    bbox: { x: 100, y: 340, width: 1, height: 100 },
    captureBbox: { x: 100, y: 340, width: 1, height: 100 }, attributes: { class: "utility sr-only" },
    style: { position: "absolute", overflowX: "hidden", overflowY: "hidden", whiteSpace: "nowrap" },
  }),
  node("sr-child", "sr-only", 350, 20),
  node("visible-rule", "css-root", 460, 100, {
    bbox: { x: 100, y: 460, width: 1, height: 100 },
    captureBbox: { x: 100, y: 460, width: 1, height: 100 }, attributes: { "aria-hidden": "true" },
  }),
  node("focused-sr", "css-root", 580, 20, { attributes: { class: "sr-only" }, style: { position: "static" } }),
  node("nonzero-opacity", "css-root", 620, 20, { style: { filter: "opacity(0.1)" } }),
];
const cssSnapshot = { ...snapshot, nodes: cssNodes, totalNodes: cssNodes.length };
const cssOriginal = JSON.stringify(cssSnapshot);
const cssResult = projectVisibleNodes(cssSnapshot);
const kept = new Set(cssResult.nodes.map(n => n.nodeId));
for (const id of ["filter-hidden", "filter-child", "clip-hidden", "clip-child", "inset-hidden", "polygon-hidden", "circle-hidden"])
  assert.equal(cssResult.excluded.find(n => n.node_id === id)?.reason, "fully_hidden_css_effect", id);
for (const id of ["sr-only", "sr-child"])
  assert.equal(cssResult.excluded.find(n => n.node_id === id)?.reason, "screen_reader_only", id);
for (const id of ["static-clip", "partial-inset", "unknown-mask", "visible-rule", "focused-sr", "nonzero-opacity"])
  assert(kept.has(id), `Must retain visible or unsupported CSS: ${id}`);
assert.equal(JSON.stringify(cssSnapshot), cssOriginal);
const cssHierarchy = buildVisualHierarchy(cssSnapshot);
assert.equal(cssHierarchy.stats.css_hidden_out, 7);
assert.equal(cssHierarchy.stats.screen_reader_only_out, 2);
console.log("PASS: empty CSS effects, ancestor propagation, sr-only evidence, visible thin rules and unknown masks");
