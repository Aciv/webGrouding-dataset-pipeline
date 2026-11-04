import type { Box } from "../types.js";
import type { HierarchyDecision } from "./types.js";

/** Shared thresholds for candidate generation, in screenshot CSS-pixel coordinates. */
export const CHILD_GEOMETRY_POLICY = Object.freeze({
  siblingIou: 0.9,
  parentSizeTolerance: 0.02,
});

/** A selected child has no materialized descendants yet; absorbed DOM sources remain traceable. */
export interface GeometryCandidate {
  node: { nodeId: string; captureBbox: Box | null };
  priority: number;
}

export interface SelectedChild<T extends GeometryCandidate = GeometryCandidate> {
  candidate: T;
  bbox: Box;
  members: string[];
  children: string[];
}

/** Intersection-over-union measures overlap, not mere containment of a small child. */
function iou(a: Box, b: Box): number {
  const area = Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)) *
    Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));
  const union = a.width * a.height + b.width * b.height - area;
  return union > 0 ? area / union : 0;
}

/** Both dimensions must be within ±2% of the parent's dimensions; position is not a criterion. */
function sameSize(child: Box, parent: Box): boolean {
  return parent.width > 0 && parent.height > 0 &&
    Math.abs(child.width / parent.width - 1) <= CHILD_GEOMETRY_POLICY.parentSizeTolerance + 1e-9 &&
    Math.abs(child.height / parent.height - 1) <= CHILD_GEOMETRY_POLICY.parentSizeTolerance + 1e-9;
}

/** Merge by union while preserving the higher-priority candidate's label and identity. */
function absorb<T extends GeometryCandidate>(a: SelectedChild<T>, b: SelectedChild<T>): SelectedChild<T> {
  const winner = b.candidate.priority > a.candidate.priority ? b : a;
  const x = Math.min(a.bbox.x, b.bbox.x), y = Math.min(a.bbox.y, b.bbox.y);
  return {
    candidate: winner.candidate,
    bbox: { x, y, width: Math.max(a.bbox.x + a.bbox.width, b.bbox.x + b.bbox.width) - x,
      height: Math.max(a.bbox.y + a.bbox.height, b.bbox.y + b.bbox.height) - y },
    members: [...a.members, ...b.members],
    children: [...a.children, ...b.children],
  };
}

/** Select one parent's children before generating the next layer.
 * Same-size candidates are omitted immediately; their not-yet-generated children
 * remain in this parent's pending queue. Only children sharing this actual parent
 * can absorb each other; equal layer numbers in different branches are insufficient.
 */
export function selectChildren<T extends GeometryCandidate>(ids: string[], parentId: string | null, parentBox: Box | null,
    candidates: Map<string, T>, children: Map<string | null, string[]>,
    decisions: Map<string, HierarchyDecision>): SelectedChild<T>[] {
  const pending = [...ids];
  const groups: SelectedChild<T>[] = [];
  for (let cursor = 0; cursor < pending.length; cursor++) {
    const id = pending[cursor];
    const candidate = candidates.get(id)!;
    const bbox = candidate.node.captureBbox!;
    if (parentBox && sameSize(bbox, parentBox)) {
      decisions.set(id, { node_id: id, status: "excluded",
        reason: "child_same_size_as_parent", represented_by: parentId });
      pending.push(...(children.get(id) ?? []));
      continue;
    }
    let group: SelectedChild<T> = { candidate, bbox: { ...bbox }, members: [id], children: [...(children.get(id) ?? [])] };
    // Absorption may change the union; reconsider other selected children until stable.
    for (let index = 0; index < groups.length;) {
      if (iou(group.bbox, groups[index].bbox) >= CHILD_GEOMETRY_POLICY.siblingIou) {
        group = absorb(groups[index], group);
        groups.splice(index, 1);
        index = 0;
      } else index++;
    }
    if (parentBox && sameSize(group.bbox, parentBox)) {
      for (const member of group.members)
        decisions.set(member, { node_id: member, status: "excluded",
          reason: "child_same_size_as_parent", represented_by: parentId });
      pending.push(...group.children);
    } else groups.push(group);
  }
  for (const group of groups) for (const id of group.members) {
    if (id === group.candidate.node.nodeId) continue;
    decisions.set(id, { node_id: id, status: "excluded",
      reason: "overlapping_sibling_absorbed", represented_by: group.candidate.node.nodeId });
  }
  return groups;
}
