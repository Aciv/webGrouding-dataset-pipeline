import type { RawSnapshot } from "../domain/types.js";
import type { VisualHierarchy } from "../domain/hierarchy/types.js";
import { STRUCT_TYPES } from "../domain/structure/index.js";
import { MIN_VISIBLE_AREA } from "../domain/visibility.js";

/** Minimal projected-box export surface; raw layout geometry may legitimately extend outside the image. */
export interface BboxList {
  image: string;
  width: number;
  height: number;
  elements: { node_id: string; bbox: number[] }[];
}

/** Check only canonical visible geometry; clipping raw DOM facts would destroy provenance. */
function validBox(box: number[], width: number, height: number): boolean {
  if (!Array.isArray(box) || box.length !== 4 || !box.every(Number.isFinite)) return false;
  const [x, y, w, h] = box;
  return x >= 0 && y >= 0 && w > 0 && h > 0 && w * h >= MIN_VISIBLE_AREA &&
    x + w <= width + .01 && y + h <= height + .01;
}

/** Validate exported geometry, labels and parent depth without claiming raw DOM provenance was checked. */
export function checkHierarchy(hierarchy: VisualHierarchy): string[] {
  const errors: string[] = [];
  const { width, height } = hierarchy.capture;
  const retained = new Map(hierarchy.nodes.map(node => [node.node_id, node]));
  if (retained.size !== hierarchy.nodes.length) errors.push("duplicate hierarchy IDs");
  for (const node of hierarchy.nodes) {
    if (!validBox(node.bbox, width, height)) errors.push(`invalid hierarchy bbox ${node.node_id}`);
    if (!(STRUCT_TYPES as readonly string[]).includes(node.struct) && node.struct !== "unknown") errors.push(`unknown struct ${node.node_id}`);
    if (!["detection", "structure_only", "review", "ignore"].includes(node.disposition) ||
        node.struct === "unknown" && node.disposition !== "review") errors.push(`invalid disposition ${node.node_id}`);
    const parent = node.parent_id ? retained.get(node.parent_id) : null;
    if (node.parent_id && !parent) errors.push(`dangling hierarchy parent ${node.node_id}`);
    if (!Number.isInteger(node.layer) || node.layer < 0 || node.layer !== (parent ? parent.layer + 1 : 0))
      errors.push(`invalid hierarchy depth ${node.node_id}`);
  }
  for (const decision of hierarchy.decisions)
    if (decision.represented_by && !retained.has(decision.represented_by))
      errors.push(`invalid representative ${decision.node_id}`);
  return errors;
}

/** Cross-check source identity, bounds, source ownership, labels and the complete parent forest. */
export function checkAnnotations(snapshot: RawSnapshot, boxes: BboxList, hierarchy: VisualHierarchy): string[] {
  const errors = checkHierarchy(hierarchy);
  const capture = snapshot.capture ?? { image: "initial.png", width: snapshot.viewport.width, height: snapshot.viewport.height };
  if (boxes.image !== capture.image || boxes.width !== capture.width || boxes.height !== capture.height)
    errors.push("bbox export image metadata mismatch");
  if (hierarchy.capture.documentId !== snapshot.documentId || hierarchy.capture.capturedAt !== snapshot.capturedAt ||
      hierarchy.capture.image !== capture.image || hierarchy.capture.width !== capture.width || hierarchy.capture.height !== capture.height)
    errors.push("hierarchy capture identity mismatch");
  const raw = new Map(snapshot.nodes.map(node => [node.nodeId, node]));
  const retained = new Map(hierarchy.nodes.map(node => [node.node_id, node]));
  if (new Set(boxes.elements.map(node => node.node_id)).size !== boxes.elements.length) errors.push("duplicate bbox IDs");
  for (const item of boxes.elements) {
    if (!raw.has(item.node_id)) errors.push(`bbox references missing source ${item.node_id}`);
    if (!validBox(item.bbox, capture.width, capture.height)) errors.push(`invalid bbox ${item.node_id}`);
  }
  for (const node of hierarchy.nodes) {
    const source = raw.get(node.node_id);
    if (!source || node.source_dom_parent_id !== source.parentId || node.tag !== source.tag) errors.push(`hierarchy source mismatch ${node.node_id}`);
    if (!Array.isArray(node.source_node_ids) || !node.source_node_ids.includes(node.node_id) || node.source_node_ids.some(id => !raw.has(id)))
      errors.push(`invalid source ownership ${node.node_id}`);

  }
  for (const decision of hierarchy.decisions)
    if (!raw.has(decision.node_id) || decision.represented_by && !retained.has(decision.represented_by))
      errors.push(`invalid representative ${decision.node_id}`);
  return errors;
}
