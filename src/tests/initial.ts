import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright";
import { loadConfig } from "../config/load.js";
import { collect } from "../application/collect.js";
import { auditBatch } from "../reporting/audit.js";
import { annotateLayers } from "../application/annotate-layers.js";
import { renderBboxes } from "../application/render-bboxes.js";
import { serveDirectory } from "../infrastructure/static-server.js";
import { serveReview } from "../review/server.js";
import type { PageResult, RawSnapshot } from "../domain/types.js";
import { readJson, writeJson } from "../infrastructure/files.js";

// A long page makes accidental full-page scrolling and out-of-viewport labels observable.
const root = await fs.mkdtemp(path.join(os.tmpdir(), "dom-initial-only-"));
const source = path.join(root, "site"), output = path.join(root, "output");
await fs.mkdir(source);
for (const [file, color, title] of [["index.html", "#992233", "Home"], ["second.html", "#113388", "Second"]]) {
  await fs.writeFile(path.join(source, file), `<!doctype html><title>${title}</title>
    <style>body{margin:0;background:${color};color:white}section{height:600px}#below{margin-top:800px}</style>
    <a href="/second.html">Second</a><h1>${title}</h1><section>Viewport content</section><button id="below">Below</button>
    <script>addEventListener('scroll',()=>{if(scrollY>0)document.body.dataset.scrolledBelow='yes'});</script>`);
}
const server = await serveDirectory(source);
let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
let review: Awaited<ReturnType<typeof serveReview>> | undefined;
try {
  const file = path.join(root, "config.json");
  const input = {
    viewports: [{ id: "mobile", width: 320, height: 240 }, { id: "desktop", width: 480, height: 320 }, { id: "tablet", width: 400, height: 300 }],
    concurrency: 1, navigationTimeoutMs: 5000, settleMs: 10, maxNodes: 1000, maxHeight: 100,
    captureMode: "initial", artifacts: { initial: true, overlayPng: false, overlaySvg: false },
    discovery: { enabled: true, minPages: 2, maxPages: 2, maxVisits: 3, menuInteractions: 0, viewport: "desktop" },
    sources: [{ id: "fixture", kind: "url", baseUrl: server.url }], pages: [{ id: "home", source: "fixture", path: "/" }],
  };
  await writeJson(file, { ...input, artifacts: { ...input.artifacts, initial: false } });
  await assert.rejects(loadConfig(file), /requires artifacts.initial=true/);
  await writeJson(file, { ...input, captureMode: "invalid" });
  await assert.rejects(loadConfig(file), /captureMode/);
  await writeJson(file, input);
  const config = await loadConfig(file);
  await collect(config, output);
  const index = await readJson<{ pages: PageResult[] }>(path.join(output, "batch-index.json"));
  assert.equal(index.pages.length, 2);
  for (const result of index.pages) {
    assert.equal(result.status, "captured");
    assert.equal(result.observations.length, 3);
    const documents = new Set<string>();
    for (const observation of result.observations) {
      const directory = path.join(output, observation.directory);
      const raw = await readJson<RawSnapshot>(path.join(directory, "nodes.json"));
      documents.add(raw.documentId);
      assert.equal(raw.capture?.image, "initial.png");
      assert.equal(raw.capture.height, raw.viewport.height);
      assert.equal(raw.capture.truncated, true);
      const image = await fs.readFile(path.join(directory, "initial.png"));
      assert.equal(image.readUInt32BE(16), raw.viewport.width);
      assert.equal(image.readUInt32BE(20), raw.viewport.height);
      for (const name of ["full.png", "overlay.png", "overlay.svg"])
        await assert.rejects(fs.access(path.join(directory, name)), { code: "ENOENT" });
      const boxes = await readJson<{ image: string; elements: { node_id: string; bbox: number[] }[] }>(path.join(directory, "bboxes.json"));
      assert.equal(boxes.image, "initial.png");
      const below = raw.nodes.find(node => node.attributes.id === "below")!;
      assert(below && !boxes.elements.some(box => box.node_id === below.nodeId));
      assert(boxes.elements.every(box => box.bbox[1] + box.bbox[3] <= raw.viewport.height + .01));
      const quality = await readJson<{ preparation: { method: string; scrollBudgetMs: number } }>(path.join(directory, "quality.json"));
      assert.equal(quality.preparation.method, "initial_viewport_v1");
      assert.equal(quality.preparation.scrollBudgetMs, 0);
      assert.notEqual(raw.nodes.find(node => node.tag === "body")?.attributes["data-scrolled-below"], "yes",
        "Initial capture must not scroll to prepare content below the viewport");
    }
    assert.equal(documents.size, 1);
  }
  assert.deepEqual((await auditBatch(output)).integrityErrors, []);
  const firstDirectory = path.join(output, index.pages[0].observations[0].directory);
  const before = await fs.readFile(path.join(firstDirectory, "nodes.json"), "utf8");
  await collect(config, output, true);
  assert.equal(await fs.readFile(path.join(firstDirectory, "nodes.json"), "utf8"), before);
  await fs.unlink(path.join(firstDirectory, "initial.png"));
  await collect(config, output, true);
  assert.deepEqual((await auditBatch(output)).integrityErrors, []);
  await annotateLayers(output);
  await renderBboxes(output);
  await assert.rejects(fs.access(path.join(firstDirectory, "full.png")), { code: "ENOENT" });
  await assert.rejects(fs.access(path.join(firstDirectory, "overlay.png")), { code: "ENOENT" });
  review = await serveReview(output, 0);
  browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(review.url);
  await page.locator("#svg rect").first().waitFor();
  assert.match(await page.locator("#image").getAttribute("src") || "", /initial\.png$/);
  console.log("PASS: initial-only three-size bundles, viewport labels, missing-image repair, resume, audit and review");
} finally {
  await browser?.close(); await review?.close(); await server.close();
  await fs.rm(root, { recursive: true, force: true });
}
