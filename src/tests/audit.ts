import assert from "node:assert/strict";
import type { RawNode, RawSnapshot } from "../domain/types.js";
import { buildVisualHierarchy } from "../domain/hierarchy/normalize.js";
import { visibleBboxes } from "../browser/bboxes.js";
import { checkAnnotations } from "../reporting/annotation-checks.js";
import { pngDimensions } from "../infrastructure/png.js";

/** Source facts include layout overflow deliberately; only canonical capture boxes must fit. */
function node(id: string, tag: string, parentId: string | null): RawNode {
  const bbox = { x: 0, y: 0, width: 400, height: 300 };
  return { nodeId: id, tag, parentId, selector: `#${id}`, attributes: {}, ownText: "", textTruncated: false,
    bbox, captureBbox: bbox, documentBbox: bbox, fragments: [bbox], textRects: [], textRectsTruncated: false,
    style: {}, interactiveHint: false, visibility: { cssVisible: true, intersectsViewport: true, clippingAncestors: [], occlusion: "not_measured" },
    pseudo: { before: "none", after: "none" } };
}
const nodes = [node("root", "main", null), node("save", "button", "root")];
nodes[1].bbox = { x: -10, y: 20, width: 100, height: 30 };
nodes[1].captureBbox = { x: 0, y: 20, width: 90, height: 30 };
nodes[1].fragments = [nodes[1].bbox];
const snapshot: RawSnapshot = { schema: "raw_dom_geometry_v1", documentId: "audit", capturedAt: "2026-10-28T00:00:00Z",
  url: "http://fixture.test/", title: "Audit", viewport: { width: 400, height: 300, dpr: 1 },
  scroll: { x: 0, y: 0 }, document: { width: 400, height: 300 }, coordinateSpace: "viewport_css_pixels",
  scope: "main_frame_light_dom", totalNodes: nodes.length, truncated: false, nodes };
const hierarchy = buildVisualHierarchy(snapshot);
const boxes = { image: "initial.png", width: 400, height: 300, elements: visibleBboxes(snapshot) };
assert.deepEqual(checkAnnotations(snapshot, boxes, hierarchy), []);
const overflow = structuredClone(boxes); overflow.elements[0].bbox[2] = 401;
assert(checkAnnotations(snapshot, overflow, hierarchy).some(error => error.includes("invalid bbox")));
const wrongCapture = structuredClone(hierarchy); wrongCapture.capture.documentId = "other";
assert(checkAnnotations(snapshot, boxes, wrongCapture).some(error => error.includes("capture identity")));
const orphan = structuredClone(hierarchy); orphan.nodes[1].parent_id = "absent";
assert(checkAnnotations(snapshot, boxes, orphan).some(error => error.includes("dangling hierarchy parent")));
const unknown = structuredClone(hierarchy); unknown.nodes[1].struct = "unknown"; unknown.nodes[1].disposition = "detection";
assert(checkAnnotations(snapshot, boxes, unknown).some(error => error.includes("invalid disposition")));
const invalidSource = structuredClone(hierarchy); invalidSource.nodes[1].source_node_ids = ["absent"];
assert(checkAnnotations(snapshot, boxes, invalidSource).some(error => error.includes("source ownership")));
assert.throws(() => pngDimensions(Buffer.alloc(24)), /Invalid PNG/);
console.log("PASS: annotation bounds, raw overflow preservation, capture identity, ownership, parent forest, labels and PNG header checks");
