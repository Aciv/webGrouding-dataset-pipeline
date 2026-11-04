import type { RawNode, RawSnapshot } from "../types.js";
import type { Descendants, StructureContext } from "./types.js";
import { isControl } from "./evidence.js";

/** Index one observation; the caller supplies its shared visibility projection. */
export function createStructureContext(snapshot: RawSnapshot, visibleNodes: readonly RawNode[]): StructureContext {
  const all = new Map(snapshot.nodes.map(node => [node.nodeId, node]));
  const visible = new Map(visibleNodes.map(node => [node.nodeId, node]));
  const children = new Map<string, RawNode[]>();
  const facts = new Map<string, Descendants>();
  const ancestorCache = new Map<string, RawNode[]>();
  for (const node of snapshot.nodes) if (node.parentId) {
    const list = children.get(node.parentId) ?? [];
    list.push(node); children.set(node.parentId, list);
  }
  /** Aggregate visible descendants across wrappers with cycle protection. */
  const summarize = (node: RawNode, visiting = new Set<string>()): Descendants => {
    const cached = facts.get(node.nodeId);
    if (cached) return cached;
    if (visiting.has(node.nodeId)) return { controls: 0, headings: 0, media: 0, children: 0 };
    visiting.add(node.nodeId);
    const result: Descendants = { controls: 0, headings: 0, media: 0, children: 0 };
    for (const child of children.get(node.nodeId) ?? []) {
      const nested = summarize(child, visiting), shown = visible.has(child.nodeId);
      result.controls += nested.controls + Number(shown && isControl(child));
      result.headings += nested.headings + Number(shown && /^h[1-6]$/.test(child.tag));
      result.media += nested.media + Number(shown && /^(img|svg|canvas|video|audio)$/.test(child.tag));
      result.children += Number(shown);
    }
    visiting.delete(node.nodeId); facts.set(node.nodeId, result);
    return result;
  };
  return {
    visibleIds: new Set(visible.keys()),
    children: node => (children.get(node.nodeId) ?? []).flatMap(child => {
      const item = visible.get(child.nodeId);
      return item ? [item] : [];
    }),
    ancestors: node => {
      const cached = ancestorCache.get(node.nodeId);
      if (cached) return cached;
      const result: RawNode[] = [], seen = new Set([node.nodeId]);
      let parent = node.parentId;
      while (parent && !seen.has(parent)) {
        seen.add(parent);
        const item = all.get(parent);
        if (!item) break;
        result.push(item); parent = item.parentId;
      }
      ancestorCache.set(node.nodeId, result);
      return result;
    },
    descendants: summarize,
  };
}
