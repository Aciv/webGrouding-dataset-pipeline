import type { RawSnapshot, RawNode } from "../types.js";
import { classifyCandidate, isControl, type Candidate } from "./candidates.js";
import type { VisualHierarchy, VisualNode, HierarchyDecision } from "./types.js";
import { projectVisibleNodes } from "../visibility.js";
import { optimizeCandidates } from "./optimizations.js";
import { createStructureContext, STRUCTURE_RULE_VERSION } from "../structure/index.js";
import { CHILD_GEOMETRY_POLICY, selectChildren } from "./child-selection.js";

export const HIERARCHY_RULE_VERSION = "dom_visual_rules_6_child_geometry";

/** Walk source ancestry with cycle protection; geometry never invents a parent. */
function ancestors(node: RawNode, all: Map<string, RawNode>): RawNode[] {
  const result: RawNode[] = [], visited = new Set([node.nodeId]);
  let parent = node.parentId;
  while (parent && !visited.has(parent)) {
    visited.add(parent);
    const item = all.get(parent);
    if (!item) break;
    result.push(item); parent = item.parentId;
  }
  return result;
}

/** Generate an unverified visual forest, compressing wrappers and atomic internals.
 * Each root has layer=0; every retained child has parent.layer+1. Source DOM stays untouched.
 */
export function buildVisualHierarchy(snapshot: RawSnapshot): VisualHierarchy {
  const all = new Map(snapshot.nodes.map(node => [node.nodeId, node]));
  const projection = projectVisibleNodes(snapshot);
  const visible = projection.nodes;
  const structureContext = createStructureContext(snapshot, visible);
  const candidates = new Map<string, Candidate>();
  const decisions = new Map<string, HierarchyDecision>();
  for (const excluded of projection.excluded)
    decisions.set(excluded.node_id, { node_id: excluded.node_id, status: "excluded", reason: excluded.reason, represented_by: null });
  for (const node of visible) {
    const { candidate, reason } = classifyCandidate(node, structureContext);
    if (candidate) candidates.set(node.nodeId, candidate);
    else decisions.set(node.nodeId, { node_id: node.nodeId, status: "excluded", reason, represented_by: null });
  }
  optimizeCandidates(snapshot, candidates, decisions);

  // Suppress all primitive descendants of complete controls/media, irrespective of their tags.
  for (const node of visible) {
    if (decisions.get(node.nodeId)?.reason.startsWith("ignore_") ||
        decisions.get(node.nodeId)?.reason === "sibling_control_icon") continue;
    const owner = ancestors(node, all).reverse().find(item => candidates.get(item.nodeId)?.atomic);
    if (!owner) continue;
    candidates.delete(node.nodeId);
    decisions.set(node.nodeId, { node_id: node.nodeId, status: "excluded", reason: "inside_atomic_object", represented_by: owner.nodeId });
  }
  // Establish candidate ancestry once. No visual descendants are created yet.
  const children = new Map<string | null, string[]>();
  for (const [id, candidate] of candidates) {
    const parentId = ancestors(candidate.node, all).find(item => candidates.has(item.nodeId))?.nodeId ?? null;
    const siblings = children.get(parentId) ?? [];
    siblings.push(id);
    children.set(parentId, siblings);
  }
  const nodes: VisualNode[] = [];
  const retained = new Map<string, VisualNode>();
  const queue: { parent: VisualNode | null; ids: string[] }[] = [{ parent: null, ids: children.get(null) ?? [] }];
  // Finalize siblings before enqueuing their children, so filtering never rewrites
  // already-generated descendants. Layers are assigned only to retained nodes.
  for (let cursor = 0; cursor < queue.length; cursor++) {
    const { parent, ids } = queue[cursor];
    const parentBox = parent ? {
      x: parent.bbox[0], y: parent.bbox[1], width: parent.bbox[2], height: parent.bbox[3],
    } : null;
    const selected = selectChildren(ids, parent?.node_id ?? null, parentBox, candidates, children, decisions);
    for (const group of selected) {
      const candidate = group.candidate;
      const id = candidate.node.nodeId;
      const rect = group.bbox;
      const fragments = candidate.node.fragments.filter(box => box.width > 0 && box.height > 0);
      const fragmentArea = fragments.reduce((sum, box) => sum + box.width * box.height, 0);
      const reasons = [...candidate.reasons];
      if (group.members.length > 1) reasons.push("absorbed_overlapping_siblings");
      const result: VisualNode = {
        ...candidate.structure,
        source_node_ids: [...new Set(group.members.flatMap(member => candidates.get(member)!.structure.source_node_ids))],
        node_id: id, parent_id: parent?.node_id ?? null, layer: parent ? parent.layer + 1 : 0,
        kind: candidate.kind, semantic_hint: candidate.hint, tag: candidate.node.tag, selector: candidate.node.selector,
        bbox: [rect.x, rect.y, rect.width, rect.height], source_dom_parent_id: candidate.node.parentId,
        layout_bbox: [all.get(id)!.bbox.x, all.get(id)!.bbox.y, all.get(id)!.bbox.width, all.get(id)!.bbox.height],
        interaction_role: candidate.structure.semantic_role && isControl(candidate.node)
          ? candidate.structure.semantic_role : null,
        fragments: fragments.map(box => [box.x, box.y, box.width, box.height]),
        geometry_warnings: fragments.length > 1 && fragmentArea /
          (candidate.node.bbox.width * candidate.node.bbox.height) < 0.3 ? ["low_fragment_occupancy"] : [],
        reasons, selection_priority: candidate.priority,
      };
      retained.set(id, result);
      nodes.push(result);
      queue.push({ parent: result, ids: group.children });
    }
  }
  for (const id of candidates.keys())
    if (!retained.has(id) && !decisions.has(id)) throw new Error(`Cyclic source hierarchy: ${id}`);
  // Resolve representatives through generation-time rejection and sibling absorption.
  for (const node of visible) {
    if (retained.has(node.nodeId)) {
      decisions.set(node.nodeId, { node_id: node.nodeId, status: "retained", reason: retained.get(node.nodeId)!.reasons.join("+"), represented_by: node.nodeId });
    } else {
      const decision = decisions.get(node.nodeId)!;
      let representative = decision.represented_by;
      const visited = new Set<string>();
      while (representative && !retained.has(representative) && !visited.has(representative)) {
        visited.add(representative);
        representative = decisions.get(representative)?.represented_by ?? null;
      }
      decision.represented_by = representative && retained.has(representative) ? representative
        : ancestors(node, all).find(item => retained.has(item.nodeId))?.nodeId ?? null;
    }
  }
  const byLayer: Record<string, number> = {}, byKind: Record<string, number> = {};
  const byStruct: Record<string, number> = {}, byDisposition: Record<string, number> = {};
  for (const node of nodes) {
    byLayer[node.layer] = (byLayer[node.layer] ?? 0) + 1;
    byKind[node.kind] = (byKind[node.kind] ?? 0) + 1;
    byStruct[node.struct] = (byStruct[node.struct] ?? 0) + 1;
    byDisposition[node.disposition] = (byDisposition[node.disposition] ?? 0) + 1;
  }
  return {
    schema: "visual_hierarchy_candidate_v1", rule_version: HIERARCHY_RULE_VERSION,
    structure_rule_version: STRUCTURE_RULE_VERSION,
    annotation_status: "rule_generated_unverified", parent_basis: "retained_dom_ancestor_with_sibling_absorption",
    geometry_policy: CHILD_GEOMETRY_POLICY,
    capture: { documentId: snapshot.documentId, capturedAt: snapshot.capturedAt, url: snapshot.url,
      image: snapshot.capture?.image ?? "initial.png", width: snapshot.capture?.width ?? snapshot.viewport.width,
      height: snapshot.capture?.height ?? snapshot.viewport.height },
    nodes, decisions: [...decisions.values()],
    ignore_regions: [...decisions.values()].filter(item => item.reason.startsWith("ignore_") &&
      !ancestors(all.get(item.node_id)!, all).some(parent => decisions.get(parent.nodeId)?.reason === item.reason))
      .map(item => {
        const rect = visible.find(node => node.nodeId === item.node_id)!.captureBbox!;
        return { node_id: item.node_id, reason: item.reason,
          bbox: [rect.x, rect.y, rect.width, rect.height] as [number, number, number, number] };
      }),
    stats: { raw_visible: projection.sourceVisible, retained: nodes.length, excluded: projection.sourceVisible - nodes.length,
      visible_after_clipping: visible.length,
      ancestor_clipped_out: projection.excluded.filter(item => item.reason === "outside_ancestor_clip").length,
      area_filtered_out: projection.excluded.filter(item => item.reason === "area_below_10").length,
      css_hidden_out: projection.excluded.filter(item => item.reason === "fully_hidden_css_effect").length,
      screen_reader_only_out: projection.excluded.filter(item => item.reason === "screen_reader_only").length,
      ancestor_clipped_partial: projection.partial,
      roots: nodes.filter(node => !node.parent_id).length, max_layer: nodes.length ? Math.max(...nodes.map(node => node.layer)) : -1,
      by_layer: byLayer, by_kind: byKind, by_struct: byStruct, by_disposition: byDisposition },
  };
}
