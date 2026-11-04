import assert from "node:assert/strict";
import type { RawNode, RawSnapshot } from "../domain/types.js";
import { projectVisibleNodes } from "../domain/visibility.js";
import { classifyStructure, createStructureContext, STRUCT_TYPES } from "../domain/structure/index.js";
import { buildVisualHierarchy } from "../domain/hierarchy/normalize.js";

/** Small source fixtures isolate tag rules, independent of browser/layout and external sites. */
function node(id: string, tag: string, parent: string | null, extra: Partial<RawNode> = {}): RawNode {
  const bbox = { x: 10, y: 10, width: 120, height: 40 };
  return { nodeId: id, parentId: parent, tag, selector: `#${id}`, attributes: {}, ownText: "",
    textTruncated: false, bbox, documentBbox: bbox, captureBbox: bbox, fragments: [bbox],
    textRects: [], textRectsTruncated: false, style: { display: "block" }, interactiveHint: false,
    visibility: { cssVisible: true, intersectsViewport: true, clippingAncestors: [], occlusion: "not_measured" },
    pseudo: { before: "none", after: "none" }, ...extra };
}
const nodes = [
  node("root", "main", null, { bbox: { x: 0, y: 0, width: 900, height: 1500 },
    captureBbox: { x: 0, y: 0, width: 900, height: 1500 } }),
  node("paragraph", "p", "root", { ownText: "Plain prose" }),
  node("heading", "h2", "root", { ownText: "Heading" }),
  node("image", "img", "root"), node("svg", "svg", "root"),
  node("path", "path", "svg"), node("video", "video", "root"),
  node("button", "button", "root"), node("inside-button", "span", "button", { ownText: "Save" }),
  node("input", "input", "root", { attributes: { type: "search" } }),
  node("checkbox", "input", "root", { attributes: { type: "checkbox" } }),
  node("select", "select", "root"), node("option", "option", "select", { ownText: "One" }),
  node("multi-select", "select", "root", { attributes: { multiple: "" } }),
  node("file-input", "input", "root", { attributes: { type: "file" } }),
  node("number-input", "input", "root", { attributes: { type: "number" } }),
  node("hidden-input", "input", "root", { attributes: { type: "hidden" } }),
  node("role-button", "div", "root", { attributes: { role: "button" } }),
  node("role-option", "div", "root", { attributes: { role: "option" }, ownText: "Pick" }),
  node("fake-class", "div", "root", { attributes: { class: "button card nav" }, ownText: "Plain text" }),
  node("empty-href", "a", "root", { attributes: { href: "" }, ownText: "Reload" }),
  node("linked-picture", "a", "root", { attributes: { href: "/picture" }, style: { display: "inline" } }),
  node("picture", "picture", "linked-picture", { style: { display: "inline" } }),
  node("picture-image", "img", "picture", { bbox: { x: 20, y: 50, width: 120, height: 100 },
    captureBbox: { x: 20, y: 50, width: 120, height: 100 } }),
  node("linked-graphic", "a", "root", { attributes: { href: "/chart" } }),
  node("graphic-child", "svg", "linked-graphic"),
  node("table", "table", "root"), node("row", "tr", "table"),
  node("cell", "td", "row", { ownText: "Cell value" }),
  node("list", "ul", "root"), node("item", "li", "list", { ownText: "List text" }),
  node("painted-block", "x-dashboard-card", "root", { style: { backgroundColor: "white" } }),
  node("wrapper", "div", "root"),
  node("unknown", "custom-control", "root", { interactiveHint: true }),
  node("embed", "iframe", "root"),
  node("article", "article", "root"), node("article-header", "header", "article"),
  node("article-title", "h3", "article-header", { ownText: "Article title" }),
  node("mixed", "p", "root", { ownText: "Read more " }),
  node("mixed-link", "a", "mixed", { attributes: { href: "/more" }, ownText: "here" }),
  node("pointer-wrapper", "div", "root", { interactiveHint: true }),
  node("nested-control", "button", "pointer-wrapper", { bbox: { x: 20, y: 20, width: 80, height: 25 },
    captureBbox: { x: 20, y: 20, width: 80, height: 25 } }),
];
const snapshot: RawSnapshot = {
  schema: "raw_dom_geometry_v1", documentId: "structure", url: "http://fixture.test/", title: "Structure",
  capturedAt: "2026-10-28T00:00:00Z", viewport: { width: 900, height: 600, dpr: 1 },
  capture: { image: "full.png", width: 900, height: 1500, maxHeight: 8000, truncated: false },
  scroll: { x: 0, y: 0 }, document: { width: 900, height: 1500 }, coordinateSpace: "viewport_css_pixels",
  scope: "main_frame_light_dom", totalNodes: nodes.length, truncated: false, nodes,
};
const before = JSON.stringify(snapshot);
const visible = projectVisibleNodes(snapshot).nodes;
const context = createStructureContext(snapshot, visible);
const label = (id: string) => classifyStructure(nodes.find(n => n.nodeId === id)!, context).label;
for (const [id, struct] of [["paragraph", "text"], ["image", "image"], ["svg", "graphic"],
  ["video", "media"], ["button", "control"], ["table", "table"], ["list", "list"],
  ["root", "region"], ["painted-block", "block"]]) assert.equal(label(id)?.struct, struct);
assert.equal(label("wrapper"), null, "Empty layout wrappers cannot become generic block positives");
assert.equal(label("unknown")?.struct, "unknown");
assert.equal(label("unknown")?.disposition, "review");
assert.equal(label("embed")?.struct, "unknown");
assert.equal(label("embed")?.disposition, "review");
assert.equal(label("role-button")?.struct, "control");
assert.equal(label("role-option")?.struct, "control");
assert.equal(label("fake-class")?.struct, "text", "Class names alone cannot prove button/card/navigation identity");
assert.equal(label("empty-href")?.semantic_role, "link");
assert.equal(label("input")?.subtype, "input_search");
assert.equal(label("input")?.semantic_role, "searchbox");
assert.equal(label("checkbox")?.semantic_role, "checkbox");
assert.equal(label("select")?.subtype, "select");
assert.equal(label("multi-select")?.semantic_role, "listbox");
assert.equal(label("file-input")?.semantic_role, "input");
assert.equal(label("number-input")?.semantic_role, "spinbutton");
assert.equal(label("hidden-input"), null);
assert.equal(label("option"), null);
assert.equal(label("heading")?.text_role, "heading");
assert.equal(label("article-header")?.struct, "block");
assert.equal(label("article-header")?.subtype, "article_header");
assert.equal(label("row")?.disposition, "structure_only");
assert.equal(label("cell")?.struct, "text");
assert.equal(label("mixed")?.disposition, "review");
assert.equal(label("linked-picture")?.struct, "image");
assert.deepEqual(label("linked-picture")?.source_node_ids, ["linked-picture", "picture", "picture-image"]);
assert.equal(label("linked-graphic")?.struct, "graphic");
const hierarchy = buildVisualHierarchy(snapshot), map = new Map(hierarchy.nodes.map(n => [n.node_id, n]));
assert.equal(hierarchy.structure_rule_version, "dom_structure_v1");
assert.equal(map.get("linked-picture")?.struct, "image");
assert.deepEqual(map.get("linked-picture")?.bbox, [20, 50, 120, 100]);
assert(!map.has("picture") && !map.has("picture-image"));
assert(!map.has("path") && !map.has("inside-button"));
assert(map.has("nested-control"), "Weak pointer-wrapper evidence must not suppress genuine child controls");
assert.equal(Object.values(hierarchy.stats.by_struct).reduce((s, count) => s + count, 0), hierarchy.nodes.length);
assert.equal(Object.values(hierarchy.stats.by_disposition).reduce((s, count) => s + count, 0), hierarchy.nodes.length);
for (const n of hierarchy.nodes) {
  assert(STRUCT_TYPES.includes(n.struct as typeof STRUCT_TYPES[number]) || n.struct === "unknown");
  assert(n.struct !== "unknown" || n.disposition === "review");
  assert.equal(n.layer, n.parent_id ? map.get(n.parent_id)!.layer + 1 : 0);
  for (const id of n.source_node_ids) assert(nodes.some(source => source.nodeId === id));
}
assert.equal(JSON.stringify(snapshot), before);
console.log("PASS: nine structs, block/unknown/excluded separation, role priority, media ownership, table structure and integrated provenance");
