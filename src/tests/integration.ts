import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright";
import { collect } from "../application/collect.js";
import type { Config, RawSnapshot } from "../domain/types.js";
import { serveReview } from "../review/server.js";
import { auditBatch } from "../reporting/audit.js";
import { projectVisibleNodes } from "../domain/visibility.js";

/** Deterministic real-browser test: dense facts, identity across resize, files and inspector. */
async function test(): Promise<void> {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "ui-dom-pilot-"));
  const source = path.join(directory, "site"), output = path.join(directory, "output");
  await fs.mkdir(source);
  await fs.writeFile(path.join(source, "index.html"), `<!doctype html><html><title>Fixture</title>
    <style>body{margin:0}.panel{padding:20px;border:1px solid black}#hidden{display:none}
    @media(max-width:600px){#navigation{display:none}}</style><body>
    <nav id="navigation"><a href="/other.html">Home</a></nav>
    <main class="panel"><h1>Dense DOM</h1><button id="submit"><span>Save</span></button>
    <p id="decorative" aria-hidden="true">Visually present</p><p id="hidden">Not painted</p>
    <p>Independent plain text</p><section><p>Nested layer text</p></section></main>
    <div style="height:1600px;position:relative">
      <p id="below" style="position:absolute;top:900px">Below the initial viewport</p>
      <p id="outside" style="position:absolute;top:1400px">Outside the capture limit</p>
      <img id="lazy" width="40" height="40" style="position:absolute;top:900px" alt="lazy fixture">
    </div>
    <div id="scroll-test" style="position:absolute;top:200px;left:180px;width:180px;height:80px;overflow:auto">
      <div style="height:300px;position:relative"><p id="clipped-marker" style="position:absolute;top:120px">Unpainted scroll content</p></div>
    </div>
    <div id="filter-hidden" style="position:absolute;top:120px;left:20px;filter:opacity(0)">
      <span id="filter-child">Transparent ancestor text</span>
    </div>
    <div id="clip-hidden" style="position:absolute;top:140px;left:20px;clip:rect(0px,0px,0px,0px)">Clipped text</div>
    <div id="path-hidden" style="position:absolute;top:160px;left:20px;clip-path:inset(100%)">Clipped by path</div>
    <span id="screenreader" class="sr-only" style="position:absolute;top:180px;left:20px;width:1px;height:100px;overflow:hidden;white-space:nowrap">Screen reader text</span>
    <section id="layer-fixture" style="position:absolute;top:400px;left:0;width:200px;height:80px">
      <section style="width:150px;height:60px"><p style="width:80px;height:20px;margin:0">Layer fixture</p></section>
    </section>
    <div id="thin-rule" aria-hidden="true" style="position:absolute;top:300px;left:20px;width:1px;height:100px;background:red"></div>
    <script>addEventListener('scroll',()=>{const image=document.getElementById('lazy');
      if(scrollY>0&&!image.hasAttribute('src'))image.src='data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="40" height="40"%3E%3Crect width="40" height="40" fill="green"/%3E%3C/svg%3E';});</script>
    </body></html>`);
  const config: Config = {
    viewports: [{ id: "desktop", width: 900, height: 600 }, { id: "mobile", width: 390, height: 844 }],
    sources: [{ id: "fixture", kind: "static", root: source }],
    pages: [{ id: "home", source: "fixture", path: "/" }],
    concurrency: 1, navigationTimeoutMs: 5000, settleMs: 10, maxNodes: 1000, maxHeight: 1500,
  };
  try {
    await collect(config, output);
    const read = async (viewport: string): Promise<RawSnapshot> =>
      JSON.parse(await fs.readFile(path.join(output, "pages/home", viewport, "nodes.json"), "utf8"));
    const desktop = await read("desktop"), mobile = await read("mobile");
    const node = (snapshot: RawSnapshot, id: string) => snapshot.nodes.find(n => n.attributes.id === id)!;
    assert.equal(desktop.documentId, mobile.documentId);
    assert.equal(node(desktop, "submit").nodeId, node(mobile, "submit").nodeId);
    assert.equal(node(desktop, "hidden").nodeId, node(mobile, "hidden").nodeId);
    assert.equal(node(desktop, "navigation").visibility.cssVisible, true);
    assert.equal(node(mobile, "navigation").visibility.cssVisible, false);
    assert.equal(node(mobile, "decorative").visibility.cssVisible, true);
    assert.equal(node(mobile, "hidden").visibility.cssVisible, false);
    assert(desktop.nodes.some(n => n.tag === "p" && n.textRects.length > 0));
    assert.equal(desktop.nodes.find(n => n.tag === "span")?.parentId, node(desktop, "submit").nodeId);
    const image = await fs.readFile(path.join(output, "pages/home/mobile/initial.png"));
    assert.equal(image.readUInt32BE(16), 390);
    assert.equal(image.readUInt32BE(20), 844);
    const full = await fs.readFile(path.join(output, "pages/home/mobile/full.png"));
    assert.equal(full.readUInt32BE(16), 390);
    assert.equal(full.readUInt32BE(20), 1500);
    assert.equal(mobile.capture?.truncated, true);
    assert.equal(node(mobile, "below").visibility.intersectsViewport, false);
    assert(node(mobile, "below").captureBbox, "Below-fold elements must have full-image boxes");
    assert.equal(node(mobile, "outside").captureBbox, null);
    const quality = JSON.parse(await fs.readFile(path.join(output, "pages/home/mobile/quality.json"), "utf8"));
    assert(quality.readiness.some((image: { status: string }) => image.status === "decoded"));
    const boxes = JSON.parse(await fs.readFile(path.join(output, "pages/home/mobile/bboxes.json"), "utf8"));
    assert.equal(boxes.image, "full.png");
    assert.equal(boxes.coordinate_system, "document_css_pixels");
    assert.equal(boxes.height, config.maxHeight);
    const expected = projectVisibleNodes(mobile).nodes;
    assert.equal(boxes.elements.length, expected.length);
    assert.equal(node(mobile, "clipped-marker").visibility.cssVisible, true, "CSS visibility can be true for scroll-clipped content");
    assert(!boxes.elements.some((n: { selector: string }) => n.selector === "#clipped-marker"));
    for (const tag of ["main", "p", "span", "button"])
      assert(boxes.elements.some((n: { tag: string }) => n.tag === tag), `Missing non-filtered tag: ${tag}`);
    assert(!boxes.elements.some((n: { selector: string }) => n.selector === "#hidden"));
    assert(boxes.elements.some((n: { selector: string }) => n.selector === "#decorative"));
    for (const id of ["filter-hidden", "filter-child", "clip-hidden", "path-hidden", "screenreader"])
      assert(!boxes.elements.some((n: { selector: string }) => n.selector === `#${id}`), `Hidden CSS exported: ${id}`);
    assert(boxes.elements.some((n: { selector: string }) => n.selector === "#thin-rule"), "Visible 1px rules must remain");
    assert.equal(node(mobile, "filter-hidden").style.filter, "opacity(0)");
    assert.equal(node(mobile, "screenreader").style.whiteSpace, "nowrap");
    const overlay = await fs.readFile(path.join(output, "pages/home/mobile/overlay.png"));
    assert.equal(overlay.readUInt32BE(16), 390);
    assert.equal(overlay.readUInt32BE(20), 1500);
    await collect(config, output, true);
    assert.equal((await read("mobile")).documentId, mobile.documentId, "Resume must preserve completed files");
    const summary = await auditBatch(output);
    assert.equal(summary.observations, 2);
    assert.deepEqual(summary.integrityErrors, []);
    const server = await serveReview(output, 0);
    const browser = await chromium.launch();
    try {
      const page = await browser.newPage();
      const errors: string[] = [];
      page.on("pageerror", error => errors.push(error.message));
      await page.goto(server.url);
      await page.locator("#svg rect").first().waitFor();
      assert.match(await page.locator("#image").getAttribute("src") ?? "", /full\.png$/);
      assert.equal(await page.locator("#jobs button").count(), 1);
      const hierarchy = JSON.parse(await fs.readFile(path.join(output, "pages/home/desktop/hierarchy.json"), "utf8"));
      assert.equal(await page.locator("#box-source").inputValue(), "visual");
      assert.equal(await page.locator('input[data-layer="1"]').isChecked(), true);
      assert.equal(await page.locator("#svg rect").count(), hierarchy.nodes.filter((n: { layer: number }) => n.layer === 1).length);
      await page.locator('input[data-layer="1"]').uncheck();
      await page.locator('input[data-layer="0"]').check();
      assert.equal(await page.locator("#svg rect").count(), hierarchy.stats.by_layer[0]);
      await page.locator('input[data-layer="2"]').check();
      assert.equal(await page.locator("#svg rect").count(), hierarchy.stats.by_layer[0] + hierarchy.stats.by_layer[2], "Nonconsecutive layers must be combined");
      await page.locator("#layers-none").click();
      assert.equal(await page.locator("#svg rect").count(), 0);
      await page.locator("#layers-all").click();
      assert.equal(await page.locator("#svg rect").count(), hierarchy.nodes.length);
      await page.locator("#struct-filter").selectOption("control");
      assert.equal(await page.locator("#svg rect").count(), hierarchy.nodes.filter((n: { struct: string }) => n.struct === "control").length);
      await page.locator("#struct-filter").selectOption("");
      await page.locator("#disposition-filter").selectOption("structure_only");
      assert.equal(await page.locator("#svg rect").count(), hierarchy.nodes.filter((n: { disposition: string }) => n.disposition === "structure_only").length);
      await page.locator("#disposition-filter").selectOption("");
      await page.locator("#box-source").selectOption("raw");
      assert.equal(await page.locator('input[data-layer="1"]').isDisabled(), true);
      assert.equal(await page.locator("#struct-filter").isDisabled(), true);
      assert.equal(await page.locator("#svg rect").count(), projectVisibleNodes(desktop).nodes.length);
      await page.locator("#box-source").selectOption("visual");
      await page.locator("#layers-none").click();
      await page.locator('input[data-layer="1"]').check();
      await page.locator("#tag").selectOption("button");
      assert.equal(await page.locator("#svg rect").count(), 1);
      await page.locator("details:has(#nodes) > summary").click();
      await page.locator("#nodes button").first().click();
      assert.match(await page.locator("#detail").textContent() ?? "", /submit/);
      assert.match(await page.locator("#selected-hierarchy").textContent() ?? "", /Layer 1.*parent:/);
      assert.deepEqual(errors, []);
    } finally { await browser.close(); await server.close(); }
    console.log("PASS: raw DOM, hidden identity, responsive geometry, image dimensions, resume and review UI");
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
}

await test();
