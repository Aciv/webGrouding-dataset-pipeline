import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium, type Browser } from "playwright";
import { collectSite } from "../application/discovery/site.js";
import { loadDiscoveryState, createDiscoveryCheckpoint } from "../application/discovery/state.js";
import { BrowserInfrastructureError } from "../browser/discovery/browser-failure.js";
import { serveDirectory } from "../infrastructure/static-server.js";
import type { Config } from "../domain/types.js";

const root = await fs.mkdtemp(path.join(os.tmpdir(), "dom-browser-lifecycle-"));
await fs.writeFile(path.join(root, "index.html"), "<!doctype html><h1>Fixture</h1>");
let closeDuringNavigation: (() => void) | undefined;
const server = await serveDirectory(root, 0, async () => { closeDuringNavigation?.(); return false; });
const browser = await chromium.launch();
try {
  const config: Config = { viewports: [{ id: "desktop", width: 400, height: 300 }], concurrency: 2,
    navigationTimeoutMs: 3000, settleMs: 0, maxHeight: 1000, maxNodes: 100,
    discovery: { enabled: false, minPages: 1, maxPages: 1, maxVisits: 1, maxCandidates: 1,
      menuInteractions: 0, similarityThreshold: .95, viewport: "desktop" },
    sources: ["first", "second"].map(id => ({ id, kind: "url", baseUrl: server.url })),
    pages: ["first", "second"].map(id => ({ id, source: id, path: "/" })) };
  const state = await loadDiscoveryState(config, path.join(root, "mid-navigation"), false);
  const checkpoint = createDiscoveryCheckpoint(state, browser.version());
  closeDuringNavigation = () => { closeDuringNavigation = undefined; void browser.close(); };
  await assert.rejects(collectSite({ state, site: state.sites[0], source: config.sources[0],
    seed: config.pages[0], browser, checkpoint }), BrowserInfrastructureError);
  assert.equal(state.sites[0].status, "pending");
  assert.equal(state.sites[0].stopReason, "browser_unavailable");
  assert.equal(state.sites[1].status, "pending");
  await assert.rejects(collectSite({ state, site: state.sites[1], source: config.sources[1],
    seed: config.pages[1], browser, checkpoint }), BrowserInfrastructureError);
  assert.equal(state.sites[1].status, "pending", "Never consume another URL after the browser disconnected");

  const failing = { isConnected: () => true, newContext: async () => {
    throw new Error("browser.newContext: Target page, context or browser has been closed");
  } } as unknown as Browser;
  await assert.rejects(collectSite({ state, site: state.sites[0], source: config.sources[0],
    seed: config.pages[0], browser: failing, checkpoint }), BrowserInfrastructureError);
  assert.equal(state.sites[0].status, "pending", "Context creation failure is not a website failure even before disconnect notification");
  console.log("PASS: browser admission gate, real browser close mid-navigation, pending-site preservation and context-creation failure classification");
} finally {
  await browser.close().catch(() => {});
  await server.close();
  await fs.rm(root, { recursive: true, force: true });
}
