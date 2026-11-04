import fs from "node:fs/promises";
import path from "node:path";
import type { Page } from "playwright";
import type { PixelImage } from "../../domain/discovery/image-similarity.js";
import { pixelSimilarity } from "../../domain/discovery/image-similarity.js";
import { decodePixels } from "../../browser/discovery/images.js";
import type { SiteTask } from "./types.js";

/** References are scoped to at most the selected pages of one source, never a global cache. */
export type ReferenceImages = Map<string, { pixels: PixelImage; kind: "initial_memory" | "initial_png" | "full_top" }>;

/** Missing/corrupt reference files schedule that page for repair instead of failing the entire site. */
export async function restoreReferences(task: SiteTask, page: Page): Promise<ReferenceImages> {
  const result: ReferenceImages = new Map();
  for (const prior of task.state.pages.filter(p => p.source === task.source.id)) {
    if (prior.status !== "captured") continue;
    const useFull = task.state.config.artifacts?.initial === false;
    const file = path.join(task.state.root, "pages", prior.id, task.state.reference.id, useFull ? "full.png" : "initial.png");
    try {
      const pixels = await decodePixels(page, await fs.readFile(file), task.state.reference);
      result.set(prior.id, { pixels, kind: useFull ? "full_top" : "initial_png" });
    } catch (error) {
      prior.status = "partial";
      prior.error = `Reference unavailable; scheduled for repair: ${error instanceof Error ? error.message : String(error)}`;
    }
  }
  return result;
}

/** Pick the closest same-size reference, excluding the page currently being repaired. */
export function compareReferences(current: PixelImage, images: ReferenceImages, exclude?: string) {
  let best: { id: string; score: number; kind: "initial_memory" | "initial_png" | "full_top" } | undefined;
  for (const [id, image] of images) if (id !== exclude) {
    const score = pixelSimilarity(current, image.pixels);
    if (!best || score > best.score) best = { id, score, kind: image.kind };
  }
  return best;
}
