import fs from "node:fs/promises";
import path from "node:path";
import type { RawSnapshot } from "../domain/types.js";
import { buildVisualHierarchy } from "../domain/hierarchy/normalize.js";
import { writeJson } from "../infrastructure/files.js";
import type { StageTimer } from "../infrastructure/timing.js";

/** Shared layer palette for diagnostic SVGs; parent depth determines color. */
const colors = ["#2563eb", "#7c3aed", "#059669", "#d97706", "#db2777", "#0891b2"];

/** Escape site-derived strings before embedding them in XML. */
function xml(value: string): string {
  return value.replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" }[character]!));
}

/** Persist normalized candidates and one SVG per layer without rewriting raw evidence. */
export async function writeHierarchy(directory: string, snapshot: RawSnapshot,
    options: { diagnostics?: boolean; timer?: StageTimer } = {}) {
  const hierarchy = options.timer
    ? options.timer.measureSync("hierarchyMs", () => buildVisualHierarchy(snapshot))
    : buildVisualHierarchy(snapshot);
  await writeJson(path.join(directory, "hierarchy.json"), hierarchy, options.timer);
  if (options.diagnostics === false) return hierarchy;
  await fs.mkdir(path.join(directory, "layers"), { recursive: true });
  // Rebuilding fewer layers must not leave obsolete diagnostic SVGs looking current.
  const expected = new Set(Object.keys(hierarchy.stats.by_layer).map(layer => `layer-${layer}.svg`));
  for (const name of await fs.readdir(path.join(directory, "layers")))
    if (/^layer-\d+\.svg$/.test(name) && !expected.has(name))
      await fs.unlink(path.join(directory, "layers", name));
  for (const layer of Object.keys(hierarchy.stats.by_layer).map(Number)) {
    const rectangles = hierarchy.nodes.filter(node => node.layer === layer).map(node => {
      const [x, y, width, height] = node.bbox;
      return `<rect x="${x}" y="${y}" width="${width}" height="${height}" fill="none" stroke="${colors[layer % colors.length]}" stroke-width="1.5"><title>${xml(`${node.node_id} layer=${layer} parent=${node.parent_id} ${node.semantic_hint}`)}</title></rect>`;
    }).join("\n");
    const { width, height, image } = hierarchy.capture;
    await fs.writeFile(path.join(directory, "layers", `layer-${layer}.svg`),
      `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><image href="../${xml(image)}" width="${width}" height="${height}"/>${rectangles}</svg>`);
  }
  return hierarchy;
}
