import type { RawNode } from "../types.js";
import type { Disposition, StructureDecision, StructureKind, StructureLabel } from "./types.js";

/** Construct a traceable proposal without attaching mutable source fields. */
export function proposal(node: RawNode, struct: StructureLabel["struct"], subtype: string,
    kind: StructureKind, priority: number, atomic: boolean, reason: string,
    disposition: Disposition = "detection", extra: Partial<StructureLabel> = {}): StructureDecision {
  return { label: {
    struct, subtype, kind, disposition, semantic_role: null, text_role: null,
    source_node_ids: [node.nodeId], structure_reasons: [reason], ...extra,
  }, reason, priority, atomic };
}

/** Explicit rejection prevents unknown wrappers from becoming universal block positives. */
export function rejection(reason: string): StructureDecision { return { label: null, reason, priority: 0, atomic: false }; }
