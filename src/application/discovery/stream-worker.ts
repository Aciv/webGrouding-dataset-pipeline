/** Persistent browser executor. Python owns admission/quotas; this process owns contexts and capture. */
import readline from "node:readline";
import path from "node:path";
import { chromium } from "playwright";
import { loadConfig } from "../../config/load.js";
import { loadDiscoveryState, createDiscoveryCheckpoint } from "./state.js";
import { collectSite } from "./site.js";
import { BrowserInfrastructureError, requireBrowser } from "../../browser/discovery/browser-failure.js";

type Request = { type: "collect"; jobId: string; batchId: string; sourceId: string;
  configFile: string; output: string; resume: boolean };
type BatchContext = Awaited<ReturnType<typeof loadDiscoveryState>>;

/** JSONL control messages share stdout with existing diagnostic events, identified by their type. */
function emit(event: Record<string, unknown>): void {
  console.log(JSON.stringify(event));
}

/** Run bounded independent site jobs in ONE Chromium; a completed job immediately returns its slot. */
async function main(): Promise<void> {
  const concurrency = Number(process.argv[2]);
  const timeoutMs = Number(process.argv[3]) * 1000;
  if (!Number.isInteger(concurrency) || concurrency < 1 || !Number.isFinite(timeoutMs) || timeoutMs <= 0)
    throw new Error("stream-worker CONCURRENCY SITE_TIMEOUT_SECONDS");
  const browser = await chromium.launch({ headless: true, handleSIGINT: false, handleSIGTERM: false });
  const batches = new Map<string, Promise<{ state: BatchContext; checkpoint: () => Promise<void> }>>();
  const active = new Map<string, { batchId: string; controller: AbortController; promise: Promise<void> }>();
  const input = readline.createInterface({ input: process.stdin });
  let stopping = false, fatal = false;

  /** Browser-wide infrastructure failure must not burn the remaining candidate pool. */
  const fail = (error: unknown, category = "executor_error") => {
    if (fatal) return;
    fatal = true;
    emit({ type: "stream_fatal", category, error: String(error) });
    stop();
  };
  /** Global cancellation closes owned browser resources; normal per-site timeouts never call this. */
  function stop(): void {
    stopping = true;
    for (const item of active.values()) item.controller.abort();
    input.close();
    void browser.close().catch(() => {});
  }
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  browser.on("disconnected", () => { if (!stopping) fail("Shared Chromium disconnected", "browser_unavailable"); });

  /** Cache each chunk's existing state/checkpoint once; workers may span many chunks concurrently. */
  function batchFor(request: Request) {
    let entry = batches.get(request.batchId);
    if (!entry) {
      entry = (async () => {
        const config = await loadConfig(path.resolve(request.configFile));
        const state = await loadDiscoveryState(config, request.output, request.resume);
        return { state, checkpoint: createDiscoveryCheckpoint(state, browser.version()) };
      })();
      batches.set(request.batchId, entry);
    }
    return entry;
  }

  /** Collect exactly one source using the existing collector, then publish a durable completion event. */
  async function run(request: Request, controller: AbortController): Promise<void> {
    let timedOut = false;
    const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    try {
      const { state, checkpoint } = await batchFor(request);
      if (stopping) return;
      requireBrowser(browser);
      const site = state.sites.find(item => item.sourceId === request.sourceId);
      const source = state.config.sources.find(item => item.id === request.sourceId);
      const seed = state.config.pages.find(item => item.source === request.sourceId);
      if (!site || !source || !seed) throw new Error(`Unknown source: ${request.sourceId}`);
      if (!controller.signal.aborted && site.status !== "completed" && site.stopReason !== "invalid_entry")
        await collectSite({ state, site, source, seed, browser, checkpoint, signal: controller.signal });
      if (timedOut) {
        site.status = site.acceptedPageIds.length ? "partial" : "failed";
        site.stopReason = "site_timeout";
        site.error = `Site deadline exceeded: ${timeoutMs / 1000}s`;
        emit({ type: "site_timeout", id: site.sourceId, timeoutSeconds: timeoutMs / 1000 });
      }
      await checkpoint();
      if (stopping) return;
      requireBrowser(browser);
      emit({ type: "stream_result", jobId: request.jobId, batchId: request.batchId,
        sourceId: request.sourceId, status: site.status, error: site.error, timedOut, interrupted: stopping });
    } finally {
      clearTimeout(deadline);
    }
  }

  try {
    emit({ type: "stream_ready", executorVersion: "browser_recovery_v1", pid: process.pid,
      concurrency, browserVersion: browser.version() });
    for await (const line of input) {
      if (stopping) break;
      requireBrowser(browser);
      const request = JSON.parse(line);
      if (request.type === "shutdown") break;
      if (request.type === "release_batch") {
        if ([...active.values()].some(item => item.batchId === request.batchId))
          throw new Error("Cannot release an active batch");
        batches.delete(request.batchId);
        continue;
      }
      if (request.type !== "collect" || typeof request.jobId !== "string" || active.has(request.jobId) || active.size >= concurrency)
        throw new Error("Invalid or over-capacity dispatch");
      const controller = new AbortController();
      const promise = run(request, controller).catch(error => {
        if (!stopping) fail(error, error instanceof BrowserInfrastructureError ? "browser_unavailable" : "executor_error");
      })
        .finally(() => active.delete(request.jobId));
      active.set(request.jobId, { batchId: request.batchId, controller, promise });
    }
    await Promise.allSettled([...active.values()].map(item => item.promise));
  } finally {
    stopping = true;
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
    input.close();
    await browser.close().catch(() => {});
  }
  if (fatal) process.exitCode = 1;
}

main().catch(error => {
  emit({ type: "stream_fatal", category: error instanceof BrowserInfrastructureError ? "browser_unavailable" : "executor_error", error: String(error) });
  process.exitCode = 1;
});
