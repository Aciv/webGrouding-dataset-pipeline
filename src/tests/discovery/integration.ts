import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright";
import { collect } from "../../application/collect.js";
import { auditBatch } from "../../reporting/audit.js";
import { annotateLayers } from "../../application/annotate-layers.js";
import { renderBboxes } from "../../application/render-bboxes.js";
import { serveDirectory } from "../../infrastructure/static-server.js";
import { serveReview } from "../../review/server.js";
import { loadConfig } from "../../config/load.js";
import { candidateUrl, pageIdentity } from "../../domain/discovery/url-policy.js";
import { pixelSimilarity } from "../../domain/discovery/image-similarity.js";
import type { PageResult } from "../../domain/types.js";
import type { SiteDiscovery } from "../../domain/discovery/types.js";

assert.equal(pageIdentity("https://example.test/index.html?x=1&utm_source=a#menu"), "https://example.test/?x=1");
assert.equal(candidateUrl("https://other.test/page", "https://example.test/", "https://example.test/", "/"), null);
assert.equal(candidateUrl("/logout", "https://example.test/", "https://example.test/", "/"), null);
assert.equal(candidateUrl("/my-account/sign-in.php", "https://example.test/", "https://example.test/", "/"), null);
assert.equal(candidateUrl("/sign_up.html", "https://example.test/", "https://example.test/", "/"), null);
assert.equal(candidateUrl("/api-reference", "https://example.test/", "https://example.test/", "/"), "https://example.test/api-reference");
assert.equal(pixelSimilarity({ width: 1, height: 1, data: new Uint8Array([0, 0, 0, 255]) },
  { width: 1, height: 1, data: new Uint8Array([255, 255, 255, 255]) }), 0);

/** Local pages make duplicate images, popup-mounted links, child recursion and artifact policies measurable. */
async function test(): Promise<void> {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "dom-homepage-discovery-"));
  const site = path.join(temporary, "site"), output = path.join(temporary, "output");
  await fs.mkdir(site);
  const html = (color: string, body: string) => `<!doctype html><html><title>Fixture</title>
    <style>body{margin:0;background:${color};min-height:650px;color:white;font:18px sans-serif}a{color:white}button{padding:12px}</style>
    <body>${body}</body></html>`;
  const links = '<nav><a href="/duplicate.html">Duplicate</a><a href="/broken.html">Broken</a><a href="/blue.html">Blue</a><a href="/green.html">Green</a><a href="/fourth.html">Fourth</a><a href="/fifth.html">Fifth</a><a href="/over-limit.html">Extra</a></nav>';
  const home = html("#dd0011", '<h1>Home</h1>' + links + '<a href="https://other.invalid/">External</a><a href="#anchor">Fragment</a>');
  for (const name of ["index.html", "duplicate.html"]) await fs.writeFile(path.join(site, name), home);
  await fs.writeFile(path.join(site, "blue.html"), html("#0011dd", '<h1>Blue</h1><a href="/child-only.html">Must not recurse</a>'));
  await fs.writeFile(path.join(site, "green.html"), html("#008833", "<h1>Green</h1>"));
  await fs.writeFile(path.join(site, "over-limit.html"), html("#ee8800", "<h1>Over limit</h1>"));
  await fs.writeFile(path.join(site, "fourth.html"), html("#bb8800", "<h1>Fourth</h1>"));
  await fs.writeFile(path.join(site, "fifth.html"), html("#bb00aa", "<h1>Fifth</h1>"));
  await fs.writeFile(path.join(site, "child-only.html"), html("#aa00bb", "<h1>Child only</h1>"));
  await fs.writeFile(path.join(site, "menu.html"), html("#991144", `<button id="menu" aria-expanded="false">Menu</button>
    <script>document.querySelector('#menu').addEventListener('pointerdown',()=>{
      if(!document.querySelector('a')){const a=document.createElement('a');a.href='/menu-dest.html';a.textContent='Destination';document.body.append(a);}
    });</script>`));
  await fs.writeFile(path.join(site, "menu-dest.html"), html("#0088aa", "<h1>Destination</h1>"));
  await fs.writeFile(path.join(site, "lonely.html"), html("#223344", "<h1>Only one page</h1>"));
  const server = await serveDirectory(site);
  let review: Awaited<ReturnType<typeof serveReview>> | undefined;
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
  try {
    const input = path.join(temporary, "config.json");
    await fs.writeFile(input, JSON.stringify({
      viewports: [{ id: "mobile", width: 320, height: 240 }, { id: "desktop", width: 480, height: 320 }, { id: "tablet", width: 400, height: 300 }],
      concurrency: 2, navigationTimeoutMs: 5000, settleMs: 10, maxNodes: 1000, maxHeight: 700,
      artifacts: { initial: false, overlayPng: false, overlaySvg: false },
      discovery: { enabled: true, minPages: 2, maxPages: 3, maxVisits: 10, maxCandidates: 20, menuInteractions: 2, similarityThreshold: 0.95, viewport: "desktop" },
      sources: ["main", "menu", "lonely"].map(id => ({ id, kind: "url", baseUrl: server.url })),
      pages: [{ id: "main", source: "main", path: "/" }, { id: "menu", source: "menu", path: "/menu.html" }, { id: "lonely", source: "lonely", path: "/lonely.html" }],
    }));
    const config = await loadConfig(input);
    await collect(config, output);
    const read = async (file: string) => JSON.parse(await fs.readFile(path.join(output, file), "utf8"));
    const index = await read("batch-index.json") as { pages: PageResult[] };
    const discovery = await read("discovery-index.json") as { sites: SiteDiscovery[] };
    const main = discovery.sites.find(s => s.sourceId === "main")!;
    assert.equal(main.acceptedPageIds.length, 3);
    assert.equal(main.status, "completed");
    assert(main.visits.some(v => v.status === "duplicate_image" && v.candidate.url.endsWith("/duplicate.html") && v.similarity === 1));
    assert(main.visits.some(v => v.status === "failed" && v.candidate.url.endsWith("/broken.html")));
    assert(!main.candidates.some(c => c.url.includes("child-only")), "Child URLs must never supply discovery candidates");
    assert(!main.visits.some(v => v.candidate.url.includes("over-limit")), "Stop immediately at max accepted pages");
    const menu = discovery.sites.find(s => s.sourceId === "menu")!;
    assert.equal(menu.status, "completed");
    assert.equal(menu.acceptedPageIds.length, 2);
    assert(menu.candidates.some(c => c.method === "menu_href" && c.url.endsWith("menu-dest.html")));
    assert.equal(discovery.sites.find(s => s.sourceId === "lonely")!.status, "partial");
    assert.equal(index.pages.length, 6);
    for (const p of index.pages) {
      assert.equal(p.status, "captured"); assert(p.urlIdentity && p.title);
      assert.equal(p.observations.length, 3);
      const documents = new Set<string>();
      for (const o of p.observations) {
        assert.equal(o.status, "captured");
        for (const name of ["initial.png", "overlay.png", "overlay.svg"])
          await assert.rejects(fs.access(path.join(output, o.directory, name)), { code: "ENOENT" });
        for (const name of ["full.png", "nodes.json", "bboxes.json", "hierarchy.json"])
          await fs.access(path.join(output, o.directory, name));
        const raw = await read(o.directory + "/nodes.json"); documents.add(raw.documentId);
        assert.equal(pageIdentity(raw.url), p.urlIdentity);
      }
      assert.equal(documents.size, 1, "Same-page resize must retain a single document identity");
    }
    assert.deepEqual((await auditBatch(output)).integrityErrors, []);
    await annotateLayers(output); await renderBboxes(output);
    await assert.rejects(fs.access(path.join(output, index.pages[0].observations[0].directory, "overlay.png")), { code: "ENOENT" });
    await assert.rejects(fs.access(path.join(output, index.pages[0].observations[0].directory, "overlay.svg")), { code: "ENOENT" });
    // Completed sites are not re-downloaded. The underfilled site may retry, without changing accepted frames.
    const oldMainVisits = main.visits.length;
    const first = index.pages[0].observations[0].directory;
    const before = await fs.readFile(path.join(output, first, "nodes.json"), "utf8");
    await collect(config, output, true);
    assert.equal(await fs.readFile(path.join(output, first, "nodes.json"), "utf8"), before);
    assert.equal((await read("discovery-index.json")).sites.find((s: SiteDiscovery) => s.sourceId === "main").visits.length, oldMainVisits);
    // Repair a missing reference with initial.png disabled, preserving successful sibling pages.
    const repairPage = index.pages.find(p => p.id === "main-p001")!;
    const repairFrame = repairPage.observations.find(o => o.viewport === "desktop")!;
    const repairBefore = JSON.parse(await fs.readFile(path.join(output, repairFrame.directory, "nodes.json"), "utf8"));
    await fs.unlink(path.join(output, repairFrame.directory, "full.png"));
    const interrupted = await read("discovery-index.json");
    const interruptedSite = interrupted.sites.find((s: SiteDiscovery) => s.sourceId === "main");
    interruptedSite.visits.find((v: { pageId?: string }) => v.pageId === repairPage.id).status = "capturing";
    await fs.writeFile(path.join(output, "discovery-index.json"), JSON.stringify(interrupted));
    await collect(config, output, true);
    const repaired = await read("batch-index.json");
    assert.equal(repaired.pages.length, index.pages.length);
    assert.equal(repaired.pages.find((p: PageResult) => p.id === repairPage.id).status, "captured");
    const repairAfter = JSON.parse(await fs.readFile(path.join(output, repairFrame.directory, "nodes.json"), "utf8"));
    assert.notEqual(repairBefore.documentId, repairAfter.documentId);
    assert.equal(await fs.readFile(path.join(output, first, "nodes.json"), "utf8"), before);
    const history = (await read("discovery-index.json")).sites.find((s: SiteDiscovery) => s.sourceId === "main");
    assert(history.visits.some((v: { status: string }) => v.status === "interrupted"));
    assert.equal(history.visits.at(-1).comparisonReference, "full_top");
    assert.deepEqual((await auditBatch(output)).integrityErrors, []);
    review = await serveReview(output, 0); browser = await chromium.launch();
    const page = await browser.newPage();
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(review.url); await page.locator("#svg rect").first().waitFor();
    assert.match(await page.locator("#image").getAttribute("src") || "", /full\.png$/);
    assert.deepEqual(errors, []);
    await browser.close(); browser = undefined;
    const fiveOutput = path.join(temporary, "five-pages");
    await collect({ ...config, sources: config.sources.filter(s => s.id === "main"), pages: config.pages.filter(p => p.source === "main"),
      discovery: { ...config.discovery!, maxPages: 5 } }, fiveOutput);
    const fiveIndex = JSON.parse(await fs.readFile(path.join(fiveOutput, "batch-index.json"), "utf8"));
    const fiveDiscovery = JSON.parse(await fs.readFile(path.join(fiveOutput, "discovery-index.json"), "utf8"));
    assert.equal(fiveIndex.pages.length, 5);
    assert.equal(fiveDiscovery.sites[0].status, "completed");
    assert(!fiveDiscovery.sites[0].visits.some((v: { candidate: { url: string } }) => v.candidate.url.includes("over-limit")));
    // A resize-triggered redirect must never publish an incomplete responsive bundle.
    await fs.writeFile(path.join(site, "responsive-redirect.html"), html("#663399", `<h1>Responsive redirect</h1>
      <script>function redirect(){if(innerWidth<400)location.replace('/green.html')}
      addEventListener('resize',redirect);redirect();</script>`));
    const redirectOutput = path.join(temporary, "responsive-redirect");
    await collect({ ...config, sources: [{ id: "redirect", kind: "url", baseUrl: server.url }],
      pages: [{ id: "redirect", source: "redirect", path: "/responsive-redirect.html" }],
      discovery: { ...config.discovery!, minPages: 2, maxPages: 2, menuInteractions: 0 } }, redirectOutput);
    const redirectIndex = JSON.parse(await fs.readFile(path.join(redirectOutput, "batch-index.json"), "utf8"));
    const redirectSite = JSON.parse(await fs.readFile(path.join(redirectOutput, "discovery-index.json"), "utf8")).sites[0] as SiteDiscovery;
    assert.equal(redirectIndex.pages.length, 0, "Failed responsive bundles must not enter the dataset");
    assert.equal(redirectSite.acceptedPageIds.length, 0, "Failed bundles must not consume the page quota");
    const rejected = redirectSite.visits.find(v => v.status === "failed")!;
    assert(rejected.candidatePageId);
    assert.equal(rejected.pageId, undefined);
    assert(rejected.viewportResults?.some(o => o.status === "failed"));
    console.log("PASS: homepage-only discovery, URL/pixel dedup, hard page cap, menu mounting, partial minimum, no optional PNG/SVG, resume and viewer");
  } finally {
    await browser?.close(); await review?.close(); await server.close();
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

await test();
