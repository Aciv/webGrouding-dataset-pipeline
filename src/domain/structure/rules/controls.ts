import type { StructureRule } from "../types.js";
import { controlRole } from "../evidence.js";
import { proposal, rejection } from "../decision.js";

/** Controls own their internals; media-only and whole-card links retain content structure. */
export const controlRule: StructureRule = (node, context) => {
  if (node.tag === "input" && node.attributes.type?.toLowerCase() === "hidden") return rejection("nonvisual_hidden_input");
  const role = controlRole(node);
  if (!role) return null;
  const children = context.children(node), facts = context.descendants(node);
  if (node.tag === "a" && !node.ownText && children.length === 1 &&
      /^(img|picture|svg|canvas|video|audio)$/.test(children[0].tag)) {
    const media = children[0];
    const pictureImages = media.tag === "picture" ? context.children(media).filter(n => n.tag === "img") : [];
    const struct = /^(svg|canvas)$/.test(media.tag) ? "graphic" : /^(video|audio)$/.test(media.tag) ? "media" : "image";
    return proposal(node, struct, `linked_${struct}`, "element", 95, true, "media_link_content", "detection", {
      semantic_role: "link", source_node_ids: [node.nodeId, media.nodeId, ...pictureImages.map(n => n.nodeId)],
    });
  }
  if (node.tag === "a" && node.bbox.width > 150 && node.bbox.height > 80 &&
      (facts.headings > 0 || facts.media > 0 && facts.children > 1))
    return proposal(node, "block", "linked_content", "group", 80, false, "structured_link", "detection", { semantic_role: "link" });
  const subtype = node.tag === "input" ? `input_${(node.attributes.type || "text").toLowerCase()}` :
    node.tag === "select" ? "select" : node.tag === "textarea" ? "textarea" : role;
  const headingParent = context.ancestors(node).find(parent => /^h[1-6]$/.test(parent.tag));
  return proposal(node, "control", subtype, "element", 95, true, "complete_control", "detection", {
    semantic_role: role, text_role: headingParent ? "heading" : null,
  });
};
