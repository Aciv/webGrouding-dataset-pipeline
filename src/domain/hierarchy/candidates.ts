import type { RawNode } from "../types.js";
import { classifyStructure, type StructureContext, type StructureLabel } from "../structure/index.js";

export { isControl, paintEvidence } from "../structure/index.js";

/** Geometry/ownership adapter. All structural classification rules live in domain/structure. */
export interface Candidate {
  node: RawNode;
  kind: StructureLabel["kind"];
  hint: string;
  priority: number;
  atomic: boolean;
  reasons: string[];
  structure: StructureLabel;
}

/** Adapt a pure classification result to existing geometry and parent normalization. */
export function classifyCandidate(node: RawNode, context: StructureContext): { candidate: Candidate | null; reason: string } {
  const decision = classifyStructure(node, context);
  if (!decision.label) return { candidate: null, reason: decision.reason };
  return { candidate: { node, kind: decision.label.kind, hint: decision.label.subtype,
    priority: decision.priority, atomic: decision.atomic, reasons: [decision.reason], structure: decision.label }, reason: decision.reason };
}
