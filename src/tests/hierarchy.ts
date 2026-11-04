import assert from "node:assert/strict";
import type { RawNode, RawSnapshot } from "../domain/types.js";
import { buildVisualHierarchy } from "../domain/hierarchy/normalize.js";

/** Minimal raw fixtures isolate label rules from browser measurement. */
function node(id: string, tag: string, parent: string | null, overrides: Partial<RawNode> = {}): RawNode {
  const bbox = { x: 10, y: 10, width: 300, height: 200 };
  return { nodeId: id, tag, parentId: parent, selector: `#${id}`, attributes: {},
    ownText: "", textTruncated: false, bbox, fragments: [bbox], documentBbox: bbox,
    captureBbox: bbox, textRects: [], textRectsTruncated: false, style: { display: "block" },
    interactiveHint: false, visibility: { cssVisible: true, intersectsViewport: true, clippingAncestors: [], occlusion: "not_measured" },
    pseudo: { before: "none", after: "none" }, ...overrides };
}

const nodes = [
  node("main", "main", null, { bbox: { x: 0, y: 0, width: 900, height: 1800 }, captureBbox: { x: 0, y: 0, width: 900, height: 1800 } }),
  node("wrapper", "div", "main"),
  node("card", "div", "wrapper", { style: { display: "block", backgroundColor: "rgb(255, 255, 255)", borderTop: "1px solid black" } }),
  node("duplicate", "div", "card", { style: { display: "block", backgroundColor: "rgb(255, 255, 255)" } }),
  node("heading", "h2", "duplicate", { ownText: "Stats", bbox: { x: 20, y: 20, width: 100, height: 30 }, captureBbox: { x: 20, y: 20, width: 100, height: 30 } }),
  node("button", "button", "duplicate", { bbox: { x: 20, y: 100, width: 100, height: 44 }, captureBbox: { x: 20, y: 100, width: 100, height: 44 } }),
  node("button-text", "span", "button", { ownText: "Save" }),
  node("overlapping-button", "button", "duplicate", { bbox: { x: 80, y: 100, width: 100, height: 44 }, captureBbox: { x: 80, y: 100, width: 100, height: 44 } }),
  node("chart", "svg", "duplicate"),
  node("chart-group", "g", "chart"),
  node("chart-text", "text", "chart-group", { ownText: "23%" }),
  node("hidden", "button", "main", { visibility: { cssVisible: false, intersectsViewport: true, clippingAncestors: [], occlusion: "not_measured" } }),
];
const snapshot: RawSnapshot = { schema: "raw_dom_geometry_v1", documentId: "fixture", url: "http://example.test/",
  title: "Hierarchy", capturedAt: "2026-10-28T00:00:00Z", viewport: { width: 900, height: 600, dpr: 1 },
  scroll: { x: 0, y: 0 }, document: { width: 900, height: 1800 }, coordinateSpace: "viewport_css_pixels",
  capture: { image: "full.png", width: 900, height: 1800, maxHeight: 8000, truncated: false },
  scope: "main_frame_light_dom", totalNodes: nodes.length, truncated: false, nodes };
const original = JSON.stringify(snapshot);
const hierarchy = buildVisualHierarchy(snapshot);
const map = new Map(hierarchy.nodes.map(node => [node.node_id, node]));
assert.equal(map.get("main")?.layer, 0);
assert.equal(map.get("card")?.parent_id, "main");
assert.equal(map.get("card")?.layer, 1);
assert.equal(map.get("heading")?.parent_id, "card");
assert.equal(map.get("heading")?.layer, 2);
for (const id of ["wrapper", "duplicate", "button-text", "chart-group", "chart-text", "hidden"])
  assert.equal(map.has(id), false, `Should compress/exclude ${id}`);
assert(map.has("button") && map.has("overlapping-button"), "Overlapping siblings must remain distinct");
for (const node of hierarchy.nodes)
  assert.equal(node.layer, node.parent_id ? map.get(node.parent_id)!.layer + 1 : 0);
assert.equal(JSON.stringify(snapshot), original, "Normalization must not modify raw DOM evidence");
assert.equal(hierarchy.decisions.length, hierarchy.stats.raw_visible);
console.log("PASS: layers, nearest retained parent, wrapper compression, atomic suppression, sibling preservation and provenance");

/** Regression geometry mirrors reviewed inline-media links, fragments and sibling menu icons. */
function rectNode(id: string, tag: string, parent: string | null,
    x: number, y: number, width: number, height: number, extra: Partial<RawNode> = {}): RawNode {
  const rect = { x, y, width, height };
  return node(id, tag, parent, { bbox: rect, captureBbox: rect, documentBbox: rect,
    fragments: [rect], ...extra });
}
const regressionNodes = [
  rectNode("root", "main", null, 0, 0, 900, 1800),
  rectNode("media-link", "a", "root", 20, 150, 100, 17,
    { attributes: { href: "/image" }, style: { display: "inline" } }),
  rectNode("image", "img", "media-link", 20, 50, 100, 110),
  rectNode("tiny", "button", "root", 0, 0, 3, 3),
  rectNode("exact-ten", "button", "root", 10, 0, 2, 5),
  rectNode("p", "p", "root", 0, 200, 800, 40),
  rectNode("cta", "a", "p", 300, 200, 200, 40, { attributes: { href: "/donate" } }),
  rectNode("article", "article", "root", 0, 300, 800, 50),
  rectNode("local-header", "header", "article", 0, 300, 800, 50),
  rectNode("title", "h3", "local-header", 20, 310, 200, 30, { ownText: "Article" }),
  rectNode("menu-item", "li", "root", 0, 400, 100, 40),
  rectNode("toggle", "button", "menu-item", 0, 400, 100, 40),
  rectNode("icon-wrapper", "span", "menu-item", 80, 410, 10, 10),
  rectNode("icon", "svg", "icon-wrapper", 80, 410, 10, 10, { attributes: { "aria-hidden": "true" } }),
  rectNode("ad", "iframe", "root", 300, 500, 390, 844, { attributes: { title: "Advertisement" } }),
  rectNode("editor", "iframe", "root", 0, 500, 200, 100, { attributes: { title: "Editor" } }),
  rectNode("divider", "svg", "root", 0, 650, 900, 100, { attributes: { class: "section-wave" } }),
  rectNode("divider-path", "path", "divider", 0, 650, 900, 100),
  rectNode("chart-named", "svg", "root", 0, 760, 900, 100,
    { attributes: { role: "img", "aria-label": "Traffic chart" } }),
  rectNode("chart-path", "path", "chart-named", 0, 760, 900, 100),
  rectNode("list", "ul", "root", 0, 900, 300, 18),
  rectNode("item", "li", "list", 0, 890, 100, 34, { ownText: "One", style: { display: "inline" } }),
  rectNode("scrollport", "div", "root", 0, 1000, 100, 20,
    { style: { display: "block", overflowY: "hidden" } }),
  rectNode("text-remnant", "a", "scrollport", 0, 1015, 100, 30,
    { ownText: "Golang", attributes: { href: "/go" } }),
  rectNode("ordinary-link", "a", "root", 0, 1400, 100, 20,
    { ownText: "FAQ", attributes: { href: "/faq" } }),
];
const regressionSnapshot = { ...snapshot, nodes: regressionNodes, totalNodes: regressionNodes.length };
const before = JSON.stringify(regressionSnapshot);
const optimized = buildVisualHierarchy(regressionSnapshot);
const optimizedMap = new Map(optimized.nodes.map(n => [n.node_id, n]));
assert.deepEqual(optimizedMap.get("media-link")!.bbox, [20, 50, 100, 110]);
assert.deepEqual(optimizedMap.get("media-link")!.layout_bbox, [20, 150, 100, 17]);
assert.equal(optimizedMap.get("media-link")!.interaction_role, "link");
assert(!optimizedMap.has("image"));
assert(!optimizedMap.has("tiny"));
assert(optimizedMap.has("exact-ten"));
assert(!optimizedMap.has("p") && optimizedMap.has("cta"));
assert(optimizedMap.has("article") && !optimizedMap.has("local-header"));
assert(!optimizedMap.has("icon") && !optimizedMap.has("toggle") && optimizedMap.has("menu-item"));
assert.equal(optimized.decisions.find(n => n.node_id === "toggle")!.reason, "child_same_size_as_parent");
assert.equal(optimized.decisions.find(n => n.node_id === "icon")!.represented_by, "menu-item");
assert(!optimizedMap.has("ad") && optimizedMap.has("editor"));
assert(!optimizedMap.has("divider") && optimizedMap.has("chart-named"));
assert(!optimizedMap.has("text-remnant"));
assert(optimized.ignore_regions.some(n => n.node_id === "ad"));
assert(optimized.ignore_regions.some(n => n.node_id === "text-remnant"));
assert.deepEqual(optimizedMap.get("list")!.bbox, [0, 890, 300, 34]);
assert(optimizedMap.has("ordinary-link"), "Ordinary text links remain eligible; no deepest-layer pruning");
assert.equal(JSON.stringify(regressionSnapshot), before);
console.log("PASS: media content geometry, strict area boundary, wrappers, article context, sibling icons, ignore regions and unchanged text links");
