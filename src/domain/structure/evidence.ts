import type { RawNode } from "../types.js";

const controlRoles = new Set(["button", "link", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
  "checkbox", "radio", "switch", "combobox", "listbox", "option", "treeitem", "slider", "spinbutton", "textbox", "searchbox"]);

/** Read an explicit role token; native semantics are handled independently below. */
export function explicitRole(node: RawNode): string {
  return (node.attributes.role || "").trim().toLowerCase().split(/\s+/)[0] || "";
}

/** Strong native/role evidence; class names and cursors alone cannot prove functionality. */
export function controlRole(node: RawNode): string | null {
  const role = explicitRole(node);
  if (node.tag === "input" && node.attributes.type?.toLowerCase() === "hidden") return null;
  if (controlRoles.has(role)) return role;
  if (node.tag === "a" && Object.hasOwn(node.attributes, "href")) return "link";
  if (node.tag === "button" || node.tag === "summary") return "button";
  if (node.tag === "select") return Object.hasOwn(node.attributes, "multiple") ||
    Number(node.attributes.size || 0) > 1 ? "listbox" : "combobox";
  if (node.tag === "textarea") return "textbox";
  if (node.tag === "input") {
    const type = (node.attributes.type || "text").toLowerCase();
    if (/^(submit|reset|button|image)$/.test(type)) return "button";
    if (/^(checkbox|radio)$/.test(type)) return type;
    if (type === "range") return "slider";
    if (type === "number") return "spinbutton";
    if (type === "search") return "searchbox";
    if (/^(text|email|password|tel|url)$/.test(type)) return "textbox";
    // Color/file/date inputs are controls, but are not ordinary textboxes.
    return "input";
  }
  return null;
}

/** Shared control predicate for ownership and geometry processing. */
export function isControl(node: RawNode): boolean { return controlRole(node) !== null; }

/** Missing/zero-alpha paint does not establish an independent visual boundary. */
function transparent(value: string | undefined): boolean {
  return !value || value === "none" || value === "transparent" ||
    /rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(value) || /\/\s*0(?:\.0+)?\s*\)$/.test(value);
}

/** Paint indicates visual grouping, not a verified functional component. */
export function paintEvidence(node: RawNode): string[] {
  const style = node.style, evidence: string[] = [];
  if (!transparent(style.backgroundColor) || !transparent(style.backgroundImage)) evidence.push("painted_background");
  if ([style.borderTop, style.borderRight, style.borderBottom, style.borderLeft]
    .some(value => parseFloat(value || "0") > 0 && !/\b(none|hidden)\b/.test(value || ""))) evidence.push("border");
  if (style.boxShadow && style.boxShadow !== "none") evidence.push("shadow");
  return evidence;
}

/** URL-backed content differs from plain colors and CSS gradients. */
export function hasBackgroundImage(node: RawNode): boolean { return /url\(/i.test(node.style.backgroundImage || ""); }

/** Pseudo content can provide icon evidence, without proving its function. */
export function hasPseudoContent(node: RawNode): boolean {
  return [node.pseudo.before, node.pseudo.after]
    .some(value => value && !["none", "normal", '\"\"', "''"].includes(value));
}
