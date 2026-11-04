import fs from "node:fs/promises";
import path from "node:path";
import { chromium, type BrowserContext } from "playwright";
import type { Config, PageResult } from "../domain/types.js";
import { capturePageObservations } from "./capture-page.js";
import { collectSites } from "./discovery/collect-sites.js";
import { serveDirectory } from "../infrastructure/static-server.js";
import { writeJson } from "../infrastructure/files.js";
import { publishReview } from "../review/page.js";
import { createBatchManifest } from "../reporting/batch-manifest.js";
import { observeNetwork } from "../browser/network.js";

/** One Chromium, bounded independent page jobs, sequential responsive observations per job.
 * Resume only reuses fully captured jobs under identical effective config; failed/partial
 * pages start a fresh lifecycle, so no old WeakMap ID is reused across observations.
 */
export async function collect(config: Config, output: string, resume = false, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return;
  if (config.discovery) return collectSites(config, output, resume, signal);
  const root = path.resolve(output);
  await fs.mkdir(root, { recursive: true });
  const indexFile = path.join(root, "batch-index.json");
  let previous: { config: Config; pages: PageResult[] } | undefined;
  try { previous = JSON.parse(await fs.readFile(indexFile, "utf8")); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  if (previous && !resume) throw new Error("Output exists; choose another output or pass --resume");
  if (previous && JSON.stringify(previous.config) !== JSON.stringify(config))
    throw new Error("Resume requires the same effective config");
  const pages: PageResult[] = config.pages.map(input => {
    const old = previous?.pages.find(page => page.id === input.id);
    return old?.status === "captured" ? old : { ...input, status: "pending", observations: [] };
  });
  const startedAt = new Date().toISOString();
  const servers: Awaited<ReturnType<typeof serveDirectory>>[] = [];
  const bases = new Map<string, string>();
  let writes = Promise.resolve();
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
  const interrupt = () => { void browser?.close().catch(() => {}); };
  /** Take serialized checkpoint snapshots so two completed jobs cannot overwrite progress. */
  const checkpoint = (status: string) => {
    const payload = structuredClone(createBatchManifest({ status, startedAt, config, pages, browserVersion: browser?.version() }));
    writes = writes.then(() => writeJson(indexFile, payload));
    return writes;
  };
  await publishReview(root);
  try {
    for (const source of config.sources) {
      if (source.kind === "static") {
        const server = await serveDirectory(source.root!);
        servers.push(server); bases.set(source.id, server.url);
      } else bases.set(source.id, source.baseUrl!);
    }
    browser = await chromium.launch({ headless: true,
      ...(signal ? { handleSIGINT: false, handleSIGTERM: false } : {}) });
    signal?.addEventListener("abort", interrupt, { once: true });
    if (signal?.aborted) interrupt();
    await checkpoint("running");
    const queue = pages.filter(page => page.status !== "captured");
    const worker = async () => {
      for (;;) {
        if (signal?.aborted) return;
        const result = queue.shift();
        if (!result) return;
        const started = performance.now();
        result.status = "running";
        await checkpoint("running");
        console.log(JSON.stringify({ type: "page_started", id: result.id }));
        let context: BrowserContext | undefined;
        try {
          context = await browser!.newContext({ viewport: config.viewports[0],
            deviceScaleFactor: 1, locale: "en-US", timezoneId: "UTC", colorScheme: "light" });
          const page = await context.newPage();
          const network = observeNetwork(page);
          const url = new URL(result.path, bases.get(result.source)!);
          if (url.origin !== new URL(bases.get(result.source)!).origin)
            throw new Error("Page path escapes configured origin");
          const response = await page.goto(url.href, {
            waitUntil: "domcontentloaded", timeout: config.navigationTimeoutMs,
          });
          if (response && response.status() >= 400) throw new Error(`Navigation HTTP ${response.status()}`);
          await capturePageObservations(page, config, root, result, () => checkpoint("running"));
          if (signal?.aborted) {
            result.status = result.observations.some(item => item.status === "captured") ? "partial" : "pending";
            result.error = "Collection interrupted";
          }
          if (!config.exportOnly) await writeJson(path.join(root, "pages", result.id, "network.json"), network);
        } catch (error) {
          result.error = error instanceof Error ? error.message : String(error);
          result.status = result.observations.some(item => item.status === "captured") ? "partial" : signal?.aborted ? "pending" : "failed";
        } finally {
          await context?.close().catch(() => {});
        }
        result.elapsedMs = Math.round(performance.now() - started);
        await checkpoint("running");
        console.log(JSON.stringify({ type: "page_finished", id: result.id, status: result.status,
          observations: result.observations.length, elapsedMs: result.elapsedMs, error: result.error }));
      }
    };
    const outcomes = await Promise.allSettled(Array.from({ length: Math.min(config.concurrency, queue.length) }, worker));
    const rejected = outcomes.find(outcome => outcome.status === "rejected");
    if (rejected?.status === "rejected") throw rejected.reason;
    await checkpoint(signal?.aborted ? "interrupted" : "completed");
  } finally {
    signal?.removeEventListener("abort", interrupt);
    await browser?.close().catch(() => {});
    await Promise.all(servers.map(server => server.close()));
  }
  console.log(JSON.stringify({ type: "batch_finished", output: root,
    captured: pages.filter(page => page.status === "captured").length,
    partial: pages.filter(page => page.status === "partial").length,
    failed: pages.filter(page => page.status === "failed").length }));
}
