import type { RawNode } from "./types.js";

/** Reasons concern rendered pixels only, never accessibility-tree visibility. */
export type HiddenCssReason = "fully_hidden_css_effect" | "screen_reader_only";

/** Recognize computed zero-area legacy clip rectangles; clip applies to positioned boxes. */
function emptyLegacyClip(node: RawNode): boolean {
  if (!/^(absolute|fixed)$/.test(node.style.position || "")) return false;
  const match = /^rect\(([^)]+)\)$/i.exec(node.style.clip || "");
  if (!match) return false;
  const tokens = match[1].trim().split(/[\s,]+/);
  if (tokens.length !== 4 || !tokens.every(t => /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:px)?$/.test(t))) return false;
  const [top, right, bottom, left] = tokens.map(parseFloat);
  return bottom <= top || right <= left;
}

/** Only proven empty basic shapes; arbitrary masks, URL references and partial clips stay unknown. */
function emptyClipPath(value: string | undefined): boolean {
  const normalized = (value || "").toLowerCase().replace(/\s+/g, " ").trim();
  if (/^circle\(0(?:px|%)?(?: at [^)]+)?\)$/.test(normalized)) return true;
  const polygon = /^polygon\(([^)]+)\)$/.exec(normalized);
  if (polygon && polygon[1].split(",").length >= 3 &&
      polygon[1].split(",").every(point => /^0(?:px|%)? 0(?:px|%)?$/.test(point.trim()))) return true;
  const inset = /^inset\(([^)]+)\)$/.exec(normalized);
  if (!inset) return false;
  // Percentage-only insets avoid guessing reference-box geometry or calc()/round behavior.
  const tokens = inset[1].split(/\s+/);
  if (tokens.length < 1 || tokens.length > 4 || !tokens.every(t => /^\d+(?:\.\d+)?%$/.test(t))) return false;
  const values = tokens.map(parseFloat);
  const [top, right = top, bottom = top, left = right] = values;
  return top + bottom >= 100 || right + left >= 100;
}

/** Exclude known invisible CSS patterns while retaining visible thin rules and aria-hidden icons.
 * Missing old-snapshot properties are unknown. Callers also apply this to recorded ancestors.
 */
export function hiddenCssReason(node: RawNode): HiddenCssReason | null {
  if (node.style.display === "contents") return null;
  const style = node.style;
  if (/\bopacity\(\s*0(?:\.0+)?%?\s*\)/i.test(style.filter || "") ||
      emptyLegacyClip(node) || emptyClipPath(style.clipPath)) return "fully_hidden_css_effect";

  const marker = /(?:^|\s)(?:sr-only|sr_only|screen-reader-only|screen-reader-text|visually-hidden|visuallyhidden|a11y-only)(?:\s|$)/i;
  const skinny = node.bbox.width <= 1 || node.bbox.height <= 1;
  const positioned = /^(absolute|fixed)$/.test(style.position || "");
  const confined = /^(hidden|clip)$/.test(style.overflowX || "") && /^(hidden|clip)$/.test(style.overflowY || "");
  const wrappedOut = node.bbox.width <= 1 && node.bbox.height <= 1 || style.whiteSpace === "nowrap" ||
    (style.margin || "").split(/\s+/).some(token => parseFloat(token) < 0);
  if (marker.test(node.attributes.class || "") && skinny && positioned && confined && wrappedOut)
    return "screen_reader_only";
  return null;
}
