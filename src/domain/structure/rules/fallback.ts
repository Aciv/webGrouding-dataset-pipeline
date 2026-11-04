import type { StructureRule } from "../types.js";
import { hasBackgroundImage, hasPseudoContent, paintEvidence } from "../evidence.js";
import { proposal, rejection } from "../decision.js";

/** Block requires visible-content evidence; unknown and empty wrappers are separate outcomes. */
export const fallbackRule: StructureRule = (node, context) => {
  const facts = context.descendants(node), paint = paintEvidence(node);
  if (!facts.children && hasPseudoContent(node) && !node.ownText)
    return proposal(node, "graphic", "icon", "element", 65, true, "pseudo_content");
  if (!facts.children && hasBackgroundImage(node))
    return proposal(node, "image", "background_image", "element", 65, true, "background_image_leaf");
  if (node.ownText && !facts.children)
    return proposal(node, "text", "text", "element", 60, true, "direct_text_leaf", "detection", { text_role: "body" });
  if (paint.length && node.bbox.width >= 16 && node.bbox.height >= 12)
    return proposal(node, "block", "visual_block", facts.children ? "group" : "element", 60,
      !facts.children, "independent_painted_block", "detection", { structure_reasons: paint });
  if (/flex|grid/.test(node.style.display || "") && facts.children >= 2 &&
      ((node.style.gap || "").split(/\s+/).some(value => parseFloat(value) > 0) ||
       (node.style.padding || "").split(/\s+/).some(value => parseFloat(value) > 0)))
    return proposal(node, "block", "layout_group", "group", 40, false, "spaced_layout", "structure_only");
  if (node.ownText && facts.children)
    return proposal(node, "block", "mixed_text", "group", 45, false, "mixed_text_segmentation_pending", "review");
  if (node.interactiveHint)
    return proposal(node, "unknown", "unverified_control_hint", facts.children ? "group" : "element",
      20, !facts.children, "weak_interaction_evidence", "review");
  return rejection("no_independent_visual_evidence");
};
