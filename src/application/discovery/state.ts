import fs from "node:fs/promises";
import path from "node:path";
import type { Config, PageResult } from "../../domain/types.js";
import type { SiteDiscovery } from "../../domain/discovery/types.js";
import { candidateUrl } from "../../domain/discovery/url-policy.js";
import { readOptionalJson, writeJson } from "../../infrastructure/files.js";
import type { DiscoveryState } from "./types.js";
import { createBatchManifest } from "../../reporting/batch-manifest.js";

/** Reconcile actual required files before trusting a captured flag during resume. */
async function reconcilePage(page: PageResult, config: Config, root: string): Promise<void> {
  const safeId = /^[a-zA-Z0-9][a-zA-Z0-9_-]*$/;
  if (!safeId.test(page.id) || !config.sources.some(s => s.id === page.source) || !page.url || !page.urlIdentity)
    throw new Error(`Invalid discovery page record: ${page.id}`);
  for (const observation of page.observations) {
    if (!config.viewports.some(v => v.id === observation.viewport) ||
        observation.directory !== `pages/${page.id}/${observation.viewport}`)
      throw new Error(`Invalid observation path: ${observation.directory}`);
    if (observation.status !== "captured") continue;
    const required = config.exportOnly ? ["hierarchy.json"] : ["nodes.json", "hierarchy.json", "quality.json", "bboxes.json", "dom.html"];
    if (config.captureMode !== "initial") required.push("full.png");
    if (config.captureMode === "initial" || !config.exportOnly && config.artifacts?.initial !== false) required.push("initial.png");
    if (!config.exportOnly && config.artifacts?.overlayPng !== false) required.push("overlay.png");
    if (!config.exportOnly && config.artifacts?.overlaySvg !== false) required.push("overlay.svg");
    for (const name of required) {
      try { await fs.access(path.join(root, observation.directory, name)); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        observation.status = "failed";
        observation.error = `Missing collected artifact: ${name}`;
        break;
      }
    }
  }
  const captured = new Set(page.observations.filter(o => o.status === "captured").map(o => o.viewport));
  page.status = config.viewports.every(v => captured.has(v.id)) ? "captured" : captured.size ? "partial" : "failed";
}

/** Restore authoritative page records and recover a possibly older discovery checkpoint. */
export async function loadDiscoveryState(config: Config, output: string, resume: boolean): Promise<DiscoveryState> {
  const root = path.resolve(output);
  const old = await readOptionalJson<{ config: Config; pages: PageResult[]; startedAt: string }>(path.join(root, "batch-index.json"));
  if (old && !resume) throw new Error("Output exists; choose another output or pass --resume");
  if (old && JSON.stringify(old.config) !== JSON.stringify(config)) throw new Error("Resume requires the same effective config");
  const saved = old ? await readOptionalJson<{ sites: SiteDiscovery[] }>(path.join(root, "discovery-index.json")) : undefined;
  const settings = config.discovery!;
  const pages = old?.pages ?? [];
  if (new Set(pages.map(page => page.id)).size !== pages.length) throw new Error("Duplicate restored page IDs");
  for (const page of pages) await reconcilePage(page, config, root);
  const sites: SiteDiscovery[] = config.sources.map(source => {
    const seed = config.pages.find(p => p.source === source.id)!;
    let entryUrl = seed.path;
    let validEntry = false;
    try {
      entryUrl = new URL(seed.path, source.baseUrl!).href;
      // Apply the same page/credential/resource policy as discovered links.
      // A bad directory entry belongs to this source, never the whole batch.
      validEntry = candidateUrl(entryUrl, source.baseUrl!, source.baseUrl!, source.pathPrefix ?? "/") !== null;
    } catch { /* Keep the invalid input as diagnostic evidence without navigating. */ }
    const site: SiteDiscovery = saved?.sites.find(s => s.sourceId === source.id) ?? { sourceId: source.id, entryUrl,
      status: "pending", candidates: [{ url: entryUrl, fromUrl: null, method: "entry" }],
      inventoryComplete: false, visits: [], acceptedPageIds: [], menuEvents: [] };
    if (!validEntry) {
      site.status = "failed";
      site.stopReason = "invalid_entry";
      site.error = `Invalid or out-of-scope page entry: ${source.id} (resource, credentials, excluded action, or scope)`;
      site.candidates = [];
    }
    return site;
  });
  for (const site of sites) {
    const selected = pages.filter(p => p.source === site.sourceId);
    if (selected.filter(page => page.status === "captured").length > settings.maxPages)
      throw new Error(`Restored source exceeds page limit: ${site.sourceId}`);
    site.acceptedPageIds = selected.filter(p => p.status === "captured").map(p => p.id);
    for (const visit of site.visits)
      if (visit.status === "running" || visit.status === "capturing") visit.status = "interrupted";
    if (site.stopReason !== "invalid_entry" && old &&
        (selected.some(p => p.status !== "captured") || site.acceptedPageIds.length < settings.minPages)) site.status = "partial";
  }
  return { root, config, settings, reference: config.viewports.find(v => v.id === settings.viewport)!,
    startedAt: old?.startedAt ?? new Date().toISOString(), pages, sites,
    reservedIds: new Set([...config.pages.map(p => p.id), ...pages.map(p => p.id)]) };
}

/** Serialize immutable snapshots; page metadata is authoritative if the second file write is interrupted. */
export function createDiscoveryCheckpoint(state: DiscoveryState, browserVersion: string) {
  let writes = Promise.resolve();
  return (status = "running"): Promise<void> => {
    const updatedAt = new Date().toISOString();
    // Incomplete records may be held transiently to repair old runs, but never enter the curated page index.
    const completePages = state.pages.filter(page => page.status === "captured");
    const siteCounts = Object.fromEntries(["pending", "running", "completed", "partial", "failed"].map(s => [s, state.sites.filter(p => p.status === s).length]));
    const batch = structuredClone(createBatchManifest({ status, startedAt: state.startedAt, updatedAt,
      browserVersion, config: state.config, siteCounts, pages: completePages }));
    const discovery = structuredClone({ schema: "homepage_discovery_v1", updatedAt, settings: state.settings,
      identity: "origin_with_https_upgrade_www_aliases_no_fragment_index_aliases", comparison: "native_initial_pixel_agreement_tolerance_32", sites: state.sites });
    writes = writes.then(async () => {
      await writeJson(path.join(state.root, "batch-index.json"), batch);
      await writeJson(path.join(state.root, "discovery-index.json"), discovery);
    });
    return writes;
  };
}
