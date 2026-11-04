import type { RawNode } from "../types.js";
import type { StructureContext, StructureDecision, StructureRule } from "./types.js";
import { rejection } from "./decision.js";
import { controlRule } from "./rules/controls.js";
import { mediaRule, textRule } from "./rules/content.js";
import { regionRule, containerRule } from "./rules/containers.js";
import { fallbackRule } from "./rules/fallback.js";

/** Label policy version, independent of screenshot and geometry versions. */
export const STRUCTURE_RULE_VERSION = "dom_structure_v1";
const rules: readonly StructureRule[] = [controlRule, mediaRule, regionRule, containerRule, textRule, fallbackRule];

/** Sole tag-to-struct entry point; caller supplies shared projected geometry, never mutated. */
export function classifyStructure(node: RawNode, context: StructureContext): StructureDecision {
  if (!context.visibleIds.has(node.nodeId)) return rejection("not_capture_visible");
  if (/^(html|body|head|script|style|link|meta|title|noscript|template|br|wbr|hr|source|track|option|optgroup)$/.test(node.tag))
    return rejection("non_independent_tag");
  for (const rule of rules) {
    const decision = rule(node, context);
    if (decision) return decision;
  }
  return rejection("no_independent_visual_evidence");
}
