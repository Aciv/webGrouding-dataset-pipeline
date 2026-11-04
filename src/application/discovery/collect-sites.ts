import { chromium } from "playwright";
import type { Config } from "../../domain/types.js";
import { publishReview } from "../../review/page.js";
import { loadDiscoveryState, createDiscoveryCheckpoint } from "./state.js";
import { collectSite } from "./site.js";
import { BrowserInfrastructureError, requireBrowser } from "../../browser/discovery/browser-failure.js";

/** Batch orchestration only: one Chromium, bounded source workers, shared checkpoint and cleanup. */
export async function collectSites(config: Config, output: string, resume = false, signal?: AbortSignal): Promise<void> {
  const state = await loadDiscoveryState(config, output, resume);
  if (signal?.aborted) return;
  const browser = await chromium.launch({ headless: true,
    ...(signal ? { handleSIGINT: false, handleSIGTERM: false } : {}) });
  const checkpoint = createDiscoveryCheckpoint(state, browser.version());
  const interrupt = () => { void browser.close().catch(() => {}); };
  signal?.addEventListener("abort", interrupt, { once: true });
  if (signal?.aborted) interrupt();
  try {
    await publishReview(state.root);
    await checkpoint();
    const queue = state.sites.filter(site => site.status !== "completed" && site.stopReason !== "invalid_entry");
    let infrastructureFailure: unknown;
    const worker = async () => {
      for (;;) {
        if (signal?.aborted || infrastructureFailure) return;
        requireBrowser(browser);
        const site = queue.shift();
        if (!site) return;
        try {
          await collectSite({ state, site, browser, checkpoint, signal,
            source: config.sources.find(source => source.id === site.sourceId)!,
            seed: config.pages.find(page => page.source === site.sourceId)! });
        } catch (error) {
          if (error instanceof BrowserInfrastructureError) infrastructureFailure = error;
          throw error;
        }
      }
    };
    const outcomes = await Promise.allSettled(Array.from({ length: Math.min(config.concurrency, queue.length) }, worker));
    const failed = outcomes.find(result => result.status === "rejected");
    if (failed?.status === "rejected") throw failed.reason;
    await checkpoint(signal?.aborted ? "interrupted" : "completed");
  } finally {
    signal?.removeEventListener("abort", interrupt);
    await browser.close().catch(() => {});
  }
  console.log(JSON.stringify({ type: "batch_finished", output: state.root, pages: state.pages.length,
    sites: state.sites.map(site => ({ id: site.sourceId, status: site.status, accepted: site.acceptedPageIds.length })) }));
}
