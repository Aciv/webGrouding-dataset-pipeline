import fs from "node:fs/promises";
import path from "node:path";
import type { Config, PageResult, RawSnapshot } from "../domain/types.js";
import { writeHierarchy } from "../reporting/hierarchy-files.js";
import { writeJson } from "../infrastructure/files.js";
import { publishReview } from "../review/page.js";
import { writeOverlay } from "../review/overlay.js";

/** Annotate saved captures offline; one malformed observation does not discard the rest. */
export async function annotateLayers(root: string) {
  const index = JSON.parse(await fs.readFile(path.join(root, "batch-index.json"), "utf8")) as { pages: PageResult[]; config?: Config };
  const observations = [];
  for (const page of index.pages) for (const observation of page.observations) {
    if (observation.status !== "captured") continue;
    try {
      const directory = path.join(root, observation.directory);
      const snapshot: RawSnapshot = JSON.parse(await fs.readFile(path.join(directory, "nodes.json"), "utf8"));
      const hierarchy = await writeHierarchy(directory, snapshot);
      if (index.config?.artifacts?.overlaySvg !== false) await writeOverlay(directory, snapshot);
      observations.push({ page: page.id, viewport: observation.viewport, status: "annotated",
        file: `${observation.directory}/hierarchy.json`, ...hierarchy.stats });
    } catch (error) {
      observations.push({ page: page.id, viewport: observation.viewport, status: "failed", error: String(error) });
    }
  }
  const summary = { schema: "visual_hierarchy_batch_v1", generatedAt: new Date().toISOString(),
    annotation_status: "rule_generated_unverified", observations };
  await writeJson(path.join(root, "hierarchy-summary.json"), summary);
  await publishReview(root);
  return summary;
}
