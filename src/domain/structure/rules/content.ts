import type { StructureRule } from "../types.js";
import { explicitRole } from "../evidence.js";
import { proposal, rejection } from "../decision.js";

/** Complete graphics/media own their primitives; embedded documents remain explicitly unknown. */
export const mediaRule: StructureRule = (node, context) => {
  const tag = node.tag, role = explicitRole(node);
  if (/^(iframe|object|embed)$/.test(tag))
    return proposal(node, "unknown", "embedded_content", "element", 90, true, "unparsed_embedded_content", "review");
  if (tag === "img" || tag === "picture" || role === "img" && !/^(svg|canvas)$/.test(tag))
    return proposal(node, "image", tag === "picture" ? "picture" : "image", "element", 90, true, "atomic_image", "detection", {
      source_node_ids: [node.nodeId, ...context.children(node).filter(n => n.tag === "img").map(n => n.nodeId)],
    });
  if (/^(svg|canvas)$/.test(tag)) return proposal(node, "graphic", tag, "element", 90, true, "atomic_graphic");
  if (/^(video|audio)$/.test(tag)) return proposal(node, "media", tag, "element", 90, true,
    tag === "audio" && !Object.hasOwn(node.attributes, "controls") ? "audio_controls_unverified" : "visible_media",
    tag === "audio" && !Object.hasOwn(node.attributes, "controls") ? "review" : "detection");
  if (/(?:apexcharts-canvas|highcharts-container|echarts-for-react|plotly-graph-div)/.test(node.attributes.class || "") &&
      node.bbox.width >= 100 && node.bbox.height >= 60)
    return proposal(node, "graphic", "chart", "element", 95, true, "chart_container_hint");
  return null;
};

/** Mixed prose awaits segmentation instead of silently becoming duplicate text GT. */
export const textRule: StructureRule = (node, context) => {
  const tag = node.tag, heading = /^h[1-6]$/.test(tag) || explicitRole(node) === "heading";
  if (!heading && !/^(p|label|legend|blockquote|pre|code|address|figcaption|caption|dt|dd|td|th|li)$/.test(tag)) return null;
  const facts = context.descendants(node);
  if (facts.controls || facts.media) {
    const mixedProse = Boolean(node.ownText.trim());
    return proposal(node, "block", heading ? "heading_group" : "content_group", "group", 65, false,
      mixedProse ? "mixed_text_segmentation_pending" : "mixed_content", mixedProse ? "review" : "structure_only");
  }
  if (!node.ownText && !facts.children) return rejection("empty_text_wrapper");
  const textRole = heading ? "heading" : /^(label|legend)$/.test(tag) ? "label" : /^(pre|code)$/.test(tag) ? "code" : "body";
  return proposal(node, "text", heading ? "heading" : "text", "element", 75, true, "text_block", "detection", { text_role: textRole });
};
