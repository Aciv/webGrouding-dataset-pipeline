import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { Config, PageResult } from "../../domain/types.js";
import { loadDiscoveryState, createDiscoveryCheckpoint } from "../../application/discovery/state.js";
import { allocatePageId, validateReference } from "../../application/discovery/publication.js";
import type { SiteTask } from "../../application/discovery/types.js";
import { withinScope } from "../../domain/discovery/url-policy.js";
import { resolveDiscovery } from "../../config/discovery.js";

/** Pure persistence cases cover ID claims, mismatched checkpoints and missing-file recovery. */
const root = await fs.mkdtemp(path.join(os.tmpdir(), "dom-discovery-state-"));
const config: Config = {
  viewports: [{ id: "desktop", width: 400, height: 240 }], concurrency: 2,
  navigationTimeoutMs: 5000, settleMs: 0, maxNodes: 1000, maxHeight: 800,
  artifacts: { initial: false, overlayPng: false, overlaySvg: false },
  discovery: { enabled: true, minPages: 2, maxPages: 3, maxVisits: 8, maxCandidates: 20, menuInteractions: 0, similarityThreshold: .95, viewport: "desktop" },
  sources: [{ id: "first", kind: "url", baseUrl: "https://fixture.test/" }, { id: "second", kind: "url", baseUrl: "https://fixture.test/" }],
  pages: [{ id: "home", source: "first", path: "/" }, { id: "home-p001", source: "second", path: "/other" }],
};
try {
  assert(withinScope("http://www.example.test/", "/", "https://example.test/path"));
  assert(withinScope("https://example.test/", "/", "https://www.example.test/path"));
  assert(!withinScope("https://example.test/", "/", "http://example.test/"));
  assert(!withinScope("http://example.test/", "/", "https://other.test/"));
  assert(!withinScope("http://example.test/", "/", "https://sub.example.test/"));
  assert(!withinScope("http://example.test:8000/", "/", "https://example.test:9000/"));
  assert(!withinScope("http://example.test/allowed", "/allowed", "https://www.example.test/other"));
  assert(!withinScope("http://example.test/", "/", "https://www.example.test/?token=secret"));
  assert.equal(resolveDiscovery({}, config).maxPages, 1);
  assert.equal(resolveDiscovery({}, config).enabled, false);
  assert.equal(resolveDiscovery({ enabled: true }, config).maxPages, 5);
  assert.throws(() => resolveDiscovery({ enabled: true, minPages: 1 }, config), /2–5 pages/);
  assert.throws(() => resolveDiscovery({ enabled: true, maxPages: 6 }, config), /2–5 pages/);
  assert.throws(() => resolveDiscovery({ enabled: true, maxVisits: 1 }, config), /must cover maxPages/);
  assert.throws(() => resolveDiscovery({ viewport: "missing" }, config), /Unknown discovery.viewport/);
  assert.throws(() => resolveDiscovery({ similarityThreshold: 1.1 }, config), /similarityThreshold/);
  assert.throws(() => resolveDiscovery({}, { ...config, viewports: [{ id: "desktop", width: 4000, height: 2000 }] }), /pixel comparison budget/);
  const state = await loadDiscoveryState(config, root, false);
  const rejectedConfig: Config = { ...config,
    sources: [...config.sources, { id: "bad-pdf", kind: "url", baseUrl: "https://fixture.test/" },
      { id: "bad-origin", kind: "url", baseUrl: "https://fixture.test/" }],
    pages: [...config.pages, { id: "bad-pdf", source: "bad-pdf", path: "/article.pdf?token2=expired" },
      { id: "bad-origin", source: "bad-origin", path: "https://other.test/" }] };
  const rejected = await loadDiscoveryState(rejectedConfig, path.join(root, "invalid-entries"), false);
  assert.equal(rejected.sites[0].status, "pending");
  assert.equal(rejected.sites[2].status, "failed");
  assert.equal(rejected.sites[2].stopReason, "invalid_entry");
  assert.equal(rejected.sites[3].status, "failed");
  await createDiscoveryCheckpoint(rejected, "fixture-browser")("completed");
  const rejectedResume = await loadDiscoveryState(rejectedConfig, path.join(root, "invalid-entries"), true);
  assert.equal(rejectedResume.sites[2].status, "failed");
  assert.equal(rejectedResume.sites[2].stopReason, "invalid_entry");
  const checkpoint = createDiscoveryCheckpoint(state, "fixture-browser");
  const task: SiteTask = { state, site: state.sites[0], source: config.sources[0], seed: config.pages[0], checkpoint };
  assert.equal(allocatePageId(task, "https://fixture.test/"), "home");
  assert.equal(allocatePageId(task, "https://fixture.test/a"), "home-p002", "Child IDs must reserve other configured entry IDs");
  assert.equal(allocatePageId(task, "https://fixture.test/b"), "home-p003", "A claim takes effect before publication awaits");
  for (const [id, suffix] of [["home", "/"], ["home-p002", "/a"]]) {
    const directory = `pages/${id}/desktop`;
    await fs.mkdir(path.join(root, directory), { recursive: true });
    for (const file of ["full.png", "nodes.json", "hierarchy.json", "quality.json", "bboxes.json", "dom.html"])
      await fs.writeFile(path.join(root, directory, file), "{}");
    const record: PageResult = { id, source: "first", path: suffix, url: `https://fixture.test${suffix}`,
      urlIdentity: `https://fixture.test${suffix}`, status: "captured",
      observations: [{ viewport: "desktop", directory, status: "captured", elapsedMs: 1 }] };
    state.pages.push(record);
  }
  state.sites[0].status = "completed";
  state.sites[0].acceptedPageIds = []; // Simulate the discovery file lagging behind authoritative page metadata.
  state.sites[0].visits.push({ candidate: { url: "https://fixture.test/a", fromUrl: null, method: "entry" },
    startedAt: new Date().toISOString(), status: "capturing", pageId: "home-p002" });
  const firstWrite = checkpoint("running");
  state.sites[0].stopReason = "page_limit";
  const secondWrite = checkpoint("completed");
  await Promise.all([firstWrite, secondWrite]);
  const restored = await loadDiscoveryState(config, root, true);
  assert.deepEqual(restored.sites[0].acceptedPageIds, ["home", "home-p002"]);
  assert.equal(restored.sites[0].visits[0].status, "interrupted");
  assert.equal(restored.sites[0].stopReason, "page_limit");
  assert.equal(restored.sites[0].status, "completed");
  await fs.unlink(path.join(root, "pages/home-p002/desktop/full.png"));
  const missing = await loadDiscoveryState(config, root, true);
  assert.equal(missing.pages[1].status, "failed");
  assert.match(missing.pages[1].observations[0].error!, /Missing collected artifact: full.png/);
  assert.equal(missing.sites[0].status, "partial");
  await assert.rejects(loadDiscoveryState({ ...config, settleMs: 99 }, root, true), /same effective config/);
  await fs.mkdir(path.join(root, "stage"));
  await fs.writeFile(path.join(root, "stage/nodes.json"), JSON.stringify({ url: "https://fixture.test/changed" }));
  await assert.rejects(validateReference(task, "stage", "https://fixture.test/", "https://fixture.test/"), /URL changed/);
  const original = await fs.readFile(path.join(root, "discovery-index.json"), "utf8");
  await fs.writeFile(path.join(root, "discovery-index.json"), "{");
  await assert.rejects(loadDiscoveryState(config, root, true), SyntaxError);
  await fs.writeFile(path.join(root, "discovery-index.json"), original);
  console.log("PASS: concurrent ID reservations, checkpoint reconciliation, missing artifact repair, URL validation and corruption handling");
} finally { await fs.rm(root, { recursive: true, force: true }); }
