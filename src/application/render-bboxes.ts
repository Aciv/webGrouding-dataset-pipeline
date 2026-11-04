import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import type { Config, PageResult, RawSnapshot } from "../domain/types.js";
import { writeBboxes } from "../browser/bboxes.js";

/** Generate bbox JSON/PNG from existing DOM snapshots; never reload source websites. */
export async function renderBboxes(root: string): Promise<number> {
  const index = JSON.parse(await fs.readFile(path.join(root, "batch-index.json"), "utf8")) as { pages: PageResult[]; config?: Config };
  const browser = await chromium.launch();
  let count = 0;
  try {
    const page = await browser.newPage();
    for (const result of index.pages) {
      for (const observation of result.observations) {
        if (observation.status !== "captured") continue;
        const directory = path.join(root, observation.directory);
        const snapshot: RawSnapshot = JSON.parse(await fs.readFile(path.join(directory, "nodes.json"), "utf8"));
        await writeBboxes(page, directory, snapshot,
          await fs.readFile(path.join(directory, snapshot.capture?.image ?? "initial.png")),
          { png: index.config?.artifacts?.overlayPng });
        count++;
      }
    }
    return count;
  } finally { await browser.close(); }
}
