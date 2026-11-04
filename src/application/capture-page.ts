import type { Page } from "playwright";
import type { Config, ObservationResult, PageResult } from "../domain/types.js";
import { captureResponsive } from "../browser/responsive.js";
import { captureViewport } from "../browser/capture.js";

/** Shared per-page capture, including pre-captured discovery reference views; resize stays sequential. */
export async function capturePageObservations(page: Page, config: Config, root: string,
    result: PageResult, checkpoint: () => Promise<void>): Promise<void> {
  const completed = new Set(result.observations.filter(o => o.status === "captured").map(o => o.viewport));
  const sizes = config.viewports.filter(size => !completed.has(size.id));
  const preparation = config.sources.find(source => source.id === result.source)?.kind === "url" ? "bounded_online" : "strict";
  await captureResponsive(page, sizes, async viewport => {
    const directory = `pages/${result.id}/${viewport.id}`, started = performance.now();
    let observation: ObservationResult;
    try {
      observation = await captureViewport(page, viewport, config, root, directory, { preparation });
    } catch (error) {
      observation = { viewport: viewport.id, directory, status: "failed",
        elapsedMs: Math.round(performance.now() - started), error: error instanceof Error ? error.message : String(error) };
    }
    result.observations = result.observations.filter(item => item.viewport !== viewport.id);
    result.observations.push(observation);
    await checkpoint();
  });
  const successes = new Set(result.observations.filter(o => o.status === "captured").map(o => o.viewport));
  result.status = config.viewports.every(size => successes.has(size.id)) ? "captured" : successes.size ? "partial" : "failed";
}
