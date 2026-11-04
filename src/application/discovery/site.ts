import { discoverHomepage } from "../../browser/discovery/homepage.js";
import { openDiscoverySession, type DiscoverySession } from "../../browser/discovery/session.js";
import { pageIdentity } from "../../domain/discovery/url-policy.js";
import type { LinkCandidate } from "../../domain/discovery/types.js";
import { restoreReferences, type ReferenceImages } from "./references.js";
import { visitCandidate } from "./visit.js";
import type { SiteTask, SiteWorkerTask } from "./types.js";
import { BrowserInfrastructureError, requireBrowser } from "../../browser/discovery/browser-failure.js";

/** One bounded homepage inventory; menu replay is disabled when refreshing after capture. */
async function readHomepage(task: SiteTask, session: DiscoverySession, menus: boolean): Promise<void> {
  const { config, settings } = task.state;
  await discoverHomepage(session.page, task.site, { prefix: task.source.pathPrefix ?? "/",
    maxCandidates: settings.maxCandidates, menuInteractions: menus ? settings.menuInteractions : 0,
    desiredPages: settings.maxPages, navigationTimeoutMs: config.navigationTimeoutMs, settleMs: config.settleMs });
  await task.checkpoint();
}

/** Repair selected incomplete pages first; duplicates and already successful URLs do not restart. */
function planCandidates(task: SiteTask) {
  const selected = task.state.pages.filter(p => p.source === task.source.id);
  const existing = new Map(selected.map(p => [p.urlIdentity!, p]));
  const duplicates = new Set(task.site.visits.filter(v => v.status === "duplicate_image" || v.status === "duplicate_url")
    .map(v => pageIdentity(v.candidate.url)));
  const repair: LinkCandidate[] = selected.filter(p => p.status !== "captured")
    .map(p => ({ url: p.url!, fromUrl: task.site.entryUrl, method: "href" }));
  const todo = [...repair, ...task.site.candidates.filter(c => !existing.has(pageIdentity(c.url)) && !duplicates.has(pageIdentity(c.url)))];
  return { existing, duplicates, todo };
}

/** Source-level result describes quota and page quality independently of failed exploratory branches. */
function finishSite(task: SiteTask): void {
  const { site, state } = task;
  site.stopReason ??= site.acceptedPageIds.length >= state.settings.maxPages ? "page_limit" : "homepage_candidates_exhausted";
  const selected = state.pages.filter(p => p.source === task.source.id);
  const complete = selected.filter(p => p.status === "captured").length;
  site.status = complete >= state.settings.minPages
    ? "completed" : complete ? "partial" : "failed";
  if (complete < state.settings.minPages) {
    const latestFailure = [...site.visits].reverse().find(visit => visit.error);
    site.error = `Only ${complete} complete responsive pages; minimum is ${state.settings.minPages}` +
      (latestFailure ? `; last failure: ${latestFailure.error}` : "");
  }
}

/** Own exactly one context/page for a source; source failures cannot terminate other source jobs. */
export async function collectSite(task: SiteWorkerTask): Promise<void> {
  const { state, site } = task, { settings } = state;
  if (!task.signal?.aborted) requireBrowser(task.browser);
  site.status = "running"; delete site.error; delete site.stopReason;
  console.log(JSON.stringify({ type: "site_started", id: task.source.id, accepted: site.acceptedPageIds.length }));
  await task.checkpoint();
  let session: DiscoverySession | undefined;
  let images: ReferenceImages | undefined;
  // A site cancellation closes only its own context, never another worker's browser.
  const interrupt = () => { void session?.close().catch(() => {}); };
  task.signal?.addEventListener("abort", interrupt, { once: true });
  try {
    session = await openDiscoverySession(task.browser, state.config, task.source, state.reference, site.entryUrl);
    if (task.signal?.aborted) {
      await session.close();
      throw new Error("Site collection cancelled");
    }
    if (settings.enabled !== false && (!site.inventoryComplete || site.acceptedPageIds.length < settings.minPages)) {
      await session.navigate(site.entryUrl);
      await readHomepage(task, session, true);
    }
    images = settings.enabled === false ? new Map() : await restoreReferences(task, session.page);
    site.acceptedPageIds = state.pages.filter(p => p.source === task.source.id && p.status === "captured").map(p => p.id);
    const { existing, duplicates, todo } = planCandidates(task);
    const attempted = new Set<string>();
    let visits = 0;
    for (const candidate of todo) {
      if (task.signal?.aborted) break;
      const identity = pageIdentity(candidate.url);
      if (attempted.has(identity)) continue;
      if (site.acceptedPageIds.length >= settings.maxPages) { site.stopReason = "page_limit"; break; }
      if (visits >= settings.maxVisits) { site.stopReason = "visit_limit"; break; }
      attempted.add(identity); visits++;
      const visit = await visitCandidate(task, session, candidate, images, existing);
      if (!task.signal?.aborted) requireBrowser(task.browser);
      // Refresh only a verified homepage still open at its own URL. A responsive redirect must not leak child links.
      if (settings.enabled !== false && visit.pageId && visit.status === "accepted" &&
          pageIdentity(visit.finalUrl!) === pageIdentity(site.entryUrl) &&
          pageIdentity(session.page.url()) === pageIdentity(site.entryUrl) && site.acceptedPageIds.length < settings.maxPages) {
        await readHomepage(task, session, false);
        const queued = new Set(todo.map(c => pageIdentity(c.url)));
        todo.push(...site.candidates.filter(c => !queued.has(pageIdentity(c.url)) &&
          !attempted.has(pageIdentity(c.url)) && !existing.has(pageIdentity(c.url)) && !duplicates.has(pageIdentity(c.url))));
      }
    }
    if (task.signal?.aborted) {
      site.status = site.acceptedPageIds.length ? "partial" : "pending";
      site.stopReason = "interrupted";
    } else finishSite(task);
  } catch (error) {
    site.error = error instanceof Error ? error.message : String(error);
    if (!task.signal?.aborted && (error instanceof BrowserInfrastructureError || !task.browser.isConnected())) {
      site.status = site.acceptedPageIds.length ? "partial" : "pending";
      site.stopReason = "browser_unavailable";
      await task.checkpoint();
      throw error instanceof BrowserInfrastructureError ? error
        : new BrowserInfrastructureError(site.error, { cause: error });
    }
    site.status = site.acceptedPageIds.length ? "partial" : task.signal?.aborted ? "pending" : "failed";
    if (task.signal?.aborted) site.stopReason = "interrupted";
  } finally {
    task.signal?.removeEventListener("abort", interrupt);
    images?.clear();
    await session?.close().catch(() => {});
  }
  await task.checkpoint();
  console.log(JSON.stringify({ type: "site_finished", id: task.source.id, status: site.status,
    accepted: site.acceptedPageIds.length, stopReason: site.stopReason, error: site.error }));
}
