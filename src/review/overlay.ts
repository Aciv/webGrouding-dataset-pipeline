import fs from "node:fs/promises";
import path from "node:path";
import type { RawSnapshot } from "../domain/types.js";
import { projectVisibleNodes } from "../domain/visibility.js";

/** Escape XML attributes/text generated from untrusted page content. */
function escapeXml(value: string): string {
  return value.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&apos;" }[char]!));
}

/** Non-destructive SVG diagnostic over the untouched screenshot; no bbox NMS or labels. */
export async function writeOverlay(directory: string, snapshot: RawSnapshot): Promise<void> {
  const width = snapshot.capture?.width ?? snapshot.viewport.width;
  const height = snapshot.capture?.height ?? snapshot.viewport.height;
  const image = snapshot.capture?.image ?? "initial.png";
  const rectangles = projectVisibleNodes(snapshot).nodes
    .map(node => {
      const rect = node.captureBbox!;
      const color = "#00c853";
      return `<rect x="${rect.x}" y="${rect.y}" width="${rect.width}" height="${rect.height}" fill="none" stroke="${color}" stroke-width="1"><title>${escapeXml(`${node.nodeId} ${node.tag} ${node.ownText.slice(0, 80)}`)}</title></rect>`;
    }).join("\n");
  await fs.writeFile(path.join(directory, "overlay.svg"),
    `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><image xlink:href="${image}" width="100%" height="100%"/>${rectangles}</svg>`);
}
