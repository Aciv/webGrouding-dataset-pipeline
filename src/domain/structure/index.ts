/** Public API; no browser, hierarchy or persistence dependencies. */
export { classifyStructure, STRUCTURE_RULE_VERSION } from "./classify.js";
export { createStructureContext } from "./context.js";
export { isControl, paintEvidence } from "./evidence.js";
export { STRUCT_TYPES } from "./types.js";
export type { StructType, StructureKind, Disposition, StructureLabel, StructureDecision, StructureContext } from "./types.js";
