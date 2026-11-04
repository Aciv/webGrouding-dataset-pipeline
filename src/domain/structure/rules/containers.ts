import type { StructureRule } from "../types.js";
import { explicitRole, paintEvidence } from "../evidence.js";
import { proposal } from "../decision.js";

/** Article headers are local groups; global region detection needs visual evidence. */
export const regionRule: StructureRule = (node, context) => {
  const tag = node.tag, role = explicitRole(node);
  if (!/^(main|header|nav|aside|footer)$/.test(tag) && !/^(main|banner|navigation|complementary|contentinfo)$/.test(role)) return null;
  if (/^(header|footer)$/.test(tag) && context.ancestors(node).some(parent => parent.tag === "article"))
    return proposal(node, "block", `article_${tag}`, "group", 60, false, "article_local_group", "structure_only");
  const paint = paintEvidence(node), facts = context.descendants(node);
  const obviousNavigation = (tag === "nav" || role === "navigation") && facts.controls >= 2;
  return proposal(node, "region", role || tag, "region", 100, false, "semantic_region",
    paint.length || obviousNavigation ? "detection" : "structure_only", { semantic_role: role || tag });
};

/** Preserve rows/sections structurally without making every wrapper a detection target. */
export const containerRule: StructureRule = (node, context) => {
  const tag = node.tag, role = explicitRole(node);
  if (tag === "table" || /^(table|grid)$/.test(role))
    return proposal(node, "table", tag === "table" ? "table" : role, "group", 70, false,
      tag === "table" ? "native_table" : "explicit_table_role", tag === "table" ? "detection" : "review", { semantic_role: role || "table" });
  if (/^(thead|tbody|tfoot|tr)$/.test(tag) || /^(row|rowgroup)$/.test(role))
    return proposal(node, "block", tag === "tr" || role === "row" ? "table_row" : tag,
      "group", 70, false, "table_structure", "structure_only");
  if (/^(ul|ol|dl)$/.test(tag) || role === "list")
    return proposal(node, "list", /^(ul|ol|dl)$/.test(tag) ? tag : "list", "group", 70, false,
      "semantic_list", paintEvidence(node).length ? "detection" : "structure_only");
  if (/^(article|section|form|fieldset|details|figure)$/.test(tag) ||
      /^(dialog|alertdialog|group|tabpanel|toolbar|menu|menubar|listitem)$/.test(role)) {
    const facts = context.descendants(node), paint = paintEvidence(node);
    const composite = facts.children >= 2 && (facts.headings > 0 || facts.controls >= 2 || facts.media > 0);
    return proposal(node, "block", role || tag, "group", 70, false, "semantic_group",
      paint.length || composite ? "detection" : "structure_only", {
        semantic_role: role || (/^(form|fieldset)$/.test(tag) ? "form" : null),
      });
  }
  return null;
};
