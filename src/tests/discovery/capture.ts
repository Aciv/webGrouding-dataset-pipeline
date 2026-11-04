import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright";
import type { Config, PageResult } from "../../domain/types.js";
import { captureViewport } from "../../browser/capture.js";
import { serveDirectory } from "../../infrastructure/static-server.js";
import { writeJson, readJson } from "../../infrastructure/files.js";
import { auditBatch } from "../../reporting/audit.js";
import { capturePageObservations } from "../../application/capture-page.js";
import { extractDom } from "../../browser/dom.js";
import { openDiscoverySession } from "../../browser/discovery/session.js";
import { collect } from "../../application/collect.js";

/** One browser checks all 8 image policies plus continuously growing-page preparation. */
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "dom-capture-policies-"));
const source = path.join(temporary, "site");
await fs.mkdir(source);
await fs.writeFile(path.join(source, "steady.html"), '<!doctype html><html><title>Steady</title><style>body{margin:0;min-height:600px;background:#224466;color:white}</style><h1>Steady</h1><button>Save</button></html>');
await fs.writeFile(path.join(source, "growing.html"), `<!doctype html><html><title>Growing</title>
  <style>body{margin:0;background:#224466;color:white}</style><div id="grow" style="height:800px">Growing page</div>
  <script>addEventListener('scroll',()=>{const e=document.getElementById('grow');e.style.height=(parseFloat(e.style.height)+120)+'px';});</script></html>`);
let interruptOnRequest: AbortController | undefined;
const server = await serveDirectory(source, 0, async () => {
  if (interruptOnRequest) {
    const controller = interruptOnRequest;
    interruptOnRequest = undefined;
    queueMicrotask(() => controller.abort());
  }
  return false;
});
const browser = await chromium.launch();
try {
  const viewport = { id: "desktop", width: 320, height: 240 };
  const config: Config = { viewports: [viewport], concurrency: 1, navigationTimeoutMs: 5000,
    settleMs: 0, maxNodes: 1000, maxHeight: 10000,
    sources: [{ id: "fixture", kind: "url", baseUrl: server.url }], pages: [{ id: "steady", source: "fixture", path: "/steady.html" }] };
  // Route locally: no external network. Exercise navigation guards at the canonical HTTPS/www alias.
  const aliasSource = { id: "alias", kind: "url" as const, baseUrl: "http://www.alias.test/" };
  const alias = await openDiscoverySession(browser, config, aliasSource, viewport, aliasSource.baseUrl);
  try {
    await alias.page.route("**/*", async route => {
      await route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html><h1>Canonical home</h1>" });
    });
    await alias.navigate("https://alias.test/");
    assert.equal(alias.page.url(), "https://alias.test/");
    await alias.page.evaluate(() => Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true }));
    const before = await extractDom(alias.page, 100);
    const after = await extractDom(alias.page, 100);
    assert.equal(before.documentId, after.documentId, "Identity survives repeated extraction without browser crypto.randomUUID");
    assert(before.nodes.length > 0);
    assert.deepEqual(before.nodes.map(n => n.nodeId), after.nodes.map(n => n.nodeId));
  } finally {
    await alias.close();
  }
  const page = await browser.newPage({ viewport });
  await page.goto(server.url + "steady.html");
  for (let mask = 0; mask < 8; mask++) {
    const artifacts = { initial: Boolean(mask & 1), overlayPng: Boolean(mask & 2), overlaySvg: Boolean(mask & 4) };
    const effective = { ...config, artifacts }, root = path.join(temporary, `policy-${mask}`);
    const observation = await captureViewport(page, viewport, effective, root, "pages/steady/desktop", { preparation: "bounded_online" });
    for (const [file, keep] of [["initial.png", artifacts.initial], ["overlay.png", artifacts.overlayPng], ["overlay.svg", artifacts.overlaySvg]] as const) {
      const filePath = path.join(root, observation.directory, file);
      if (keep) await fs.access(filePath); else await assert.rejects(fs.access(filePath), { code: "ENOENT" });
    }
    assert.equal(observation.captureCounts?.initialScreenshots, artifacts.initial ? observation.captureCounts!.attempts : 0);
    assert.equal(observation.captureCounts?.domExtractions, observation.captureCounts!.attempts * 2);
    const now = new Date().toISOString();
    await writeJson(path.join(root, "batch-index.json"), { config: effective, status: "completed", startedAt: now, updatedAt: now,
      pages: [{ id: "steady", source: "fixture", path: "/steady.html", status: "captured", observations: [observation] }] });
    assert.deepEqual((await auditBatch(root)).integrityErrors, []);
  }
  const compactRoot = path.join(temporary, "compact-direct");
  const compact = await captureViewport(page, viewport, { ...config, exportOnly: true }, compactRoot,
    "pages/steady/desktop", { preparation: "bounded_online" });
  assert.deepEqual((await fs.readdir(path.join(compactRoot, compact.directory))).sort(), ["full.png", "hierarchy.json"]);
  assert.equal(compact.captureCounts!.initialScreenshots, 0);
  assert.equal(compact.captureCounts!.domExtractions, compact.captureCounts!.attempts * 2);
  assert(compact.timings!.hierarchyMs >= 0);
  assert(compact.timings!.jsonSerializeMs >= 0);
  const compactHierarchy = await readJson<{ nodes: unknown[] }>(path.join(compactRoot, compact.directory, "hierarchy.json"));
  const diagnosticHierarchy = await readJson<{ nodes: unknown[] }>(path.join(temporary, "policy-0/pages/steady/desktop/hierarchy.json"));
  assert.deepEqual(compactHierarchy.nodes, diagnosticHierarchy.nodes, "Export-only keeps identical structural labels");
  const initialCompact = await captureViewport(page, viewport,
    { ...config, exportOnly: true, captureMode: "initial" }, temporary, "compact-initial", { preparation: "bounded_online" });
  assert.deepEqual((await fs.readdir(path.join(temporary, "compact-initial"))).sort(), ["hierarchy.json", "initial.png"]);
  assert.equal(initialCompact.captureCounts!.initialScreenshots, initialCompact.captureCounts!.attempts);
  const compactBatch = path.join(temporary, "compact-batch");
  const compactConfig: Config = { ...config, exportOnly: true,
    viewports: [{ id: "mobile", width: 320, height: 240 }, { id: "desktop", width: 480, height: 320 }, { id: "tablet", width: 400, height: 300 }],
    discovery: { enabled: false, minPages: 1, maxPages: 1, maxVisits: 1, maxCandidates: 1,
      menuInteractions: 0, similarityThreshold: .95, viewport: "desktop" } };
  await collect(compactConfig, compactBatch);
  assert.deepEqual((await auditBatch(compactBatch)).integrityErrors, []);
  await collect(compactConfig, compactBatch, true);
  await fs.unlink(path.join(compactBatch, "pages/steady/mobile/full.png"));
  await collect(compactConfig, compactBatch, true);
  assert.deepEqual((await auditBatch(compactBatch)).integrityErrors, []);
  for (const size of compactConfig.viewports)
    assert.deepEqual((await fs.readdir(path.join(compactBatch, `pages/steady/${size.id}`))).sort(), ["full.png", "hierarchy.json"]);
  console.log("PASS: skipped initial screenshots, export-only label parity, two-file viewport bundles, audit and missing-image repair");
  const retry: PageResult = { id: "steady", source: "fixture", path: "/steady.html", status: "partial",
    observations: [{ viewport: "desktop", directory: "pages/steady/desktop", status: "failed", elapsedMs: 1, error: "interrupted" }] };
  await capturePageObservations(page, { ...config, artifacts: { initial: false, overlayPng: false, overlaySvg: false } },
    path.join(temporary, "retry"), retry, async () => {});
  assert.equal(retry.observations.length, 1, "Retry replaces the failed viewport record instead of appending a duplicate");
  assert.equal(retry.status, "captured");
  await page.goto(server.url + "growing.html");
  await assert.rejects(captureViewport(page, viewport, config, temporary, "strict-growing", { preparation: "strict" }), /height.*(growing|grew)/i);
  await page.goto(server.url + "growing.html");
  const result = await captureViewport(page, viewport, { ...config, artifacts: { initial: false, overlayPng: false, overlaySvg: false } },
    temporary, "online-growing", { preparation: "bounded_online" });
  assert.equal(result.status, "captured");
  const quality = await readJson<{ warnings: string[]; preparation: { method: string; capturedHeight: number } }>(path.join(temporary, "online-growing/quality.json"));
  assert.equal(quality.preparation.method, "bounded_url_capture_v1");
  assert(quality.warnings.some(warning => /height|budget/.test(warning)));
  const image = await fs.readFile(path.join(temporary, "online-growing/full.png"));
  assert.equal(image.readUInt32BE(20), quality.preparation.capturedHeight);
  await browser.close();
  const interruptedRoot = path.join(temporary, "interrupted");
  const interruptConfig: Config = { ...config, concurrency: 1,
    artifacts: { initial: false, overlayPng: false, overlaySvg: false },
    discovery: { enabled: true, minPages: 2, maxPages: 2, maxVisits: 2, maxCandidates: 2, menuInteractions: 0, similarityThreshold: .95, viewport: "desktop" },
    sources: [{ id: "first", kind: "url", baseUrl: server.url }, { id: "untouched", kind: "url", baseUrl: server.url }],
    pages: [{ id: "first", source: "first", path: "/steady.html" }, { id: "untouched", source: "untouched", path: "/steady.html" }] };
  const controller = new AbortController();
  interruptOnRequest = controller;
  await collect(interruptConfig, interruptedRoot, false, controller.signal);
  const interrupted = await readJson<{ status: string; siteCounts: Record<string, number> }>(path.join(interruptedRoot, "batch-index.json"));
  assert.equal(interrupted.status, "interrupted");
  assert.equal(interrupted.siteCounts.failed, 0);
  assert.equal(interrupted.siteCounts.pending, 2);
  await collect(interruptConfig, interruptedRoot, true);
  const resumed = await readJson<{ status: string; pages: PageResult[] }>(path.join(interruptedRoot, "batch-index.json"));
  assert.equal(resumed.status, "completed");
  assert.equal(resumed.pages.length, 2);
  console.log("PASS: eight artifact combinations, optional-aware audit, strict rejection and bounded growing-page capture");
  console.log("PASS: active navigation cancellation closes the browser, preserves pending sites and resumes cleanly");
} finally { await browser.close(); await server.close(); await fs.rm(temporary, { recursive: true, force: true }); }
