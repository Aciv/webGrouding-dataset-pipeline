import type { VisualHierarchy, VisualNode, HierarchyDecision } from "./types.js";
import { selectChildren, CHILD_GEOMETRY_POLICY } from "./child-selection.js";

/** Refine existing labels with the live child-generation rules, without DOM or pixels.
 * Older labels lack classification priority, so ties use their original order;
 * omitted historical DOM nodes cannot be recovered from an exported tree alone.
 */
export function refineSavedHierarchy(input: VisualHierarchy): VisualHierarchy {
  const saved = structuredClone(input);
  const originals = new Map(saved.nodes.map(node => [node.node_id, node]));
  if (originals.size !== saved.nodes.length) throw new Error("Duplicate saved node IDs");
  const children = new Map<string | null, string[]>();
  const candidates = new Map(saved.nodes.map(node => [node.node_id, {
    node: { nodeId: node.node_id, captureBbox: {
      x: node.bbox[0], y: node.bbox[1], width: node.bbox[2], height: node.bbox[3],
    } }, priority: node.selection_priority ?? 0,
  }]));
  for (const node of saved.nodes) {
    if (node.parent_id && !originals.has(node.parent_id)) throw new Error(`Missing saved parent: ${node.parent_id}`);
    if (node.bbox.some(value => !Number.isFinite(value)) || node.bbox[2] <= 0 || node.bbox[3] <= 0)
      throw new Error(`Invalid saved bbox: ${node.node_id}`);
    const list = children.get(node.parent_id) ?? [];
    list.push(node.node_id);
    children.set(node.parent_id, list);
  }
  const decisions = new Map<string, HierarchyDecision>(saved.decisions.map(item => [item.node_id, item]));
  const visited = new Set<string>();
  const retained = new Map<string, VisualNode>();
  const nodes: VisualNode[] = [];
  const queue: { ids: string[]; parent: VisualNode | null }[] = [{ ids: children.get(null) ?? [], parent: null }];
  for (let index = 0; index < queue.length; index++) {
    const { parent, ids } = queue[index];
    const box = parent ? { x: parent.bbox[0], y: parent.bbox[1], width: parent.bbox[2], height: parent.bbox[3] } : null;
    const selected = selectChildren(ids, parent?.node_id ?? null, box, candidates, children, decisions);
    for (const group of selected) {
      const original = originals.get(group.candidate.node.nodeId)!;
      const node: VisualNode = { ...original, parent_id: parent?.node_id ?? null,
        layer: parent ? parent.layer + 1 : 0,
        bbox: [group.bbox.x, group.bbox.y, group.bbox.width, group.bbox.height],
        source_node_ids: [...new Set(group.members.flatMap(id => originals.get(id)!.source_node_ids))],
        reasons: [...new Set([...original.reasons, ...(group.members.length > 1 ? ["absorbed_overlapping_siblings"] : [])])] };
      nodes.push(node);
      retained.set(node.node_id, node);
      decisions.set(node.node_id, { node_id: node.node_id, status: "retained", reason: node.reasons.join("+"), represented_by: node.node_id });
      queue.push({ ids: group.children, parent: node });
    }
  }
  // A disconnected candidate cycle cannot silently disappear from the result.
  for (const node of saved.nodes) {
    let id: string | null = node.node_id;
    visited.clear();
    while (id && !retained.has(id)) {
      if (visited.has(id)) throw new Error(`Cyclic saved hierarchy: ${id}`);
      visited.add(id);
      const decision = decisions.get(id);
      if (!decision || decision.status === "retained") throw new Error(`Unprocessed saved node: ${id}`);
      id = decision.represented_by;
    }
  }
  for (const decision of decisions.values()) {
    let id = decision.represented_by;
    visited.clear();
    while (id && !retained.has(id) && !visited.has(id)) {
      visited.add(id);
      id = decisions.get(id)?.represented_by ?? null;
    }
    decision.represented_by = id && retained.has(id) ? id : null;
  }
  const counts = (field: "layer" | "kind" | "struct" | "disposition") => {
    const result: Record<string, number> = {};
    for (const node of nodes) result[String(node[field])] = (result[String(node[field])] ?? 0) + 1;
    return result;
  };
  return { ...saved, rule_version: "dom_visual_rules_6_child_geometry_offline",
    geometry_policy: CHILD_GEOMETRY_POLICY, parent_basis: "retained_dom_ancestor_with_sibling_absorption",
    nodes, decisions: [...decisions.values()],
    stats: { ...saved.stats, retained: nodes.length, excluded: saved.stats.raw_visible - nodes.length,
      roots: nodes.filter(node => !node.parent_id).length, max_layer: nodes.length ? Math.max(...nodes.map(node => node.layer)) : -1,
      by_layer: counts("layer"), by_kind: counts("kind"), by_struct: counts("struct"), by_disposition: counts("disposition") } };
}
