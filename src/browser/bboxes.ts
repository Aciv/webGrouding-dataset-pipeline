import fs from "node:fs/promises";
import path from "node:path";
import type { Page } from "playwright";
import type { RawSnapshot } from "../domain/types.js";
import { writeJson } from "../infrastructure/files.js";
import { projectVisibleNodes } from "../domain/visibility.js";

/** Select every CSS-visible DOM element intersecting the screenshot, without tag filters. */
export function visibleBboxes(snapshot: RawSnapshot) {
  return projectVisibleNodes(snapshot).nodes
    .map(node => ({
      node_id: node.nodeId,
      parent_id: node.parentId,
      tag: node.tag,
      selector: node.selector,
      // Screenshot-clipped [x, y, width, height], in CSS pixels (DPR=1).
      bbox: [node.captureBbox!.x, node.captureBbox!.y,
        node.captureBbox!.width, node.captureBbox!.height],
      dom_bbox: [node.bbox.x, node.bbox.y, node.bbox.width, node.bbox.height],
    }));
}

/** Write a minimal bbox list and raster overlay over the original PNG.
 * Canvas remains detached from the source DOM; no UI, styles or interactions are injected.
 * Visibility uses CSS and screenshot intersection, not exact occlusion segmentation.
 */
export async function writeBboxes(page: Page, directory: string, snapshot: RawSnapshot,
    image: Buffer, options: { png?: boolean } = {}): Promise<void> {
  const elements = visibleBboxes(snapshot);
  await writeJson(path.join(directory, "bboxes.json"), {
    image: snapshot.capture?.image ?? "initial.png",
    width: snapshot.capture?.width ?? snapshot.viewport.width,
    height: snapshot.capture?.height ?? snapshot.viewport.height,
    coordinate_system: snapshot.capture ? "document_css_pixels" : "viewport_css_pixels",
    bbox_format: "xywh",
    selection: "all_css_visible_elements_intersecting_capture",
    visibility_policy: "css_and_ancestor_overflow_clip_area_gte_10_hidden_effects_sr_only",
    elements,
  });
  if (options.png === false) return;
  const base64 = await page.evaluate(async ({ png, boxes }) => {
    const bytes = Uint8Array.from(atob(png), char => char.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
    try {
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      const context = canvas.getContext("2d")!;
      context.drawImage(bitmap, 0, 0);
      context.strokeStyle = "#00c853";
      context.lineWidth = 1;
      for (const element of boxes) {
        const [x, y, width, height] = element.bbox;
        context.strokeRect(x, y, width, height);
      }
      return canvas.toDataURL("image/png").split(",")[1];
    } finally { bitmap.close(); }
  }, { png: image.toString("base64"), boxes: elements });
  await fs.writeFile(path.join(directory, "overlay.png"), Buffer.from(base64, "base64"));
}
