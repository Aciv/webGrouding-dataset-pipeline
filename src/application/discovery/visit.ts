import fs from "node:fs/promises";
import path from "node:path";
import type { PageResult, RawSnapshot } from "../../domain/types.js";
import type { DiscoveryVisit, LinkCandidate } from "../../domain/discovery/types.js";
import { pageIdentity } from "../../domain/discovery/url-policy.js";
import { captureViewport } from "../../browser/capture.js";
import { decodePixels } from "../../browser/discovery/images.js";
import type { DiscoverySession } from "../../browser/discovery/session.js";
import { writeJson } from "../../infrastructure/files.js";
import { StageTimer } from "../../infrastructure/timing.js";
import { capturePageObservations } from "../capture-page.js";
import { compareReferences, type ReferenceImages } from "./references.js";
import { stageReference, commitPage, validateReference, verifyResponsiveIdentity } from "./publication.js";
import type { SiteTask } from "./types.js";

/** Visit one candidate. Dataset publication happens only after URL and visual duplicate checks. */
export async function visitCandidate(task: SiteTask, session: DiscoverySession, candidate: LinkCandidate,
    images: ReferenceImages, existing: Map<string, PageResult>): Promise<DiscoveryVisit> {
  const { state, site } = task;
  const target = existing.get(pageIdentity(candidate.url));
  const started = performance.now();
  const timer = new StageTimer();
  const visit: DiscoveryVisit = { candidate, startedAt: new Date().toISOString(), status: "running" };
  site.visits.push(visit);
  session.network.length = 0;
  const staged = `.discovery-staging/${task.source.id}/visit-${site.visits.length}/${state.reference.id}`;
  let result: PageResult | undefined;
  let committed: PageResult | undefined;
  await timer.measure("checkpointMs", task.checkpoint);
  try {
    await timer.measure("navigationMs", () => session.navigate(candidate.url));
    visit.finalUrl = session.page.url();
    const actual = pageIdentity(visit.finalUrl), same = existing.get(actual);
    if (same && same !== target) {
      visit.status = "duplicate_url"; visit.duplicateOf = same.id;
    } else {
      let initial: Buffer | undefined;
      let snapshot: RawSnapshot | undefined;
      const first = await timer.measure("referenceCaptureMs", () => captureViewport(session.page, state.reference, state.config, state.root, staged,
        { preparation: "bounded_online", onSnapshot: raw => { snapshot = raw; },
          ...(state.settings.enabled === false ? {} : { onInitial: (png: Buffer) => { initial = png; } }) }));
      const raw = await validateReference(task, staged, actual, session.page.url(), snapshot);
      const current = state.settings.enabled === false ? undefined : await timer.measure("imageDecodeMs", () => decodePixels(session.page, initial!));
      const best = current ? compareReferences(current, images, target?.id) : undefined;
      if (best) {
        visit.similarity = best.score; visit.closestPageId = best.id; visit.comparisonReference = best.kind;
      }
      if (best && best.score >= state.settings.similarityThreshold) {
        visit.status = "duplicate_image"; visit.duplicateOf = best.id;
      } else {
        const previousIdentity = target?.urlIdentity;
        const stagedPage = await timer.measure("stagingMs", () => stageReference(task, visit, visit.finalUrl!, staged, first, raw, target));
        result = stagedPage.result;
        const progress = () => {
          visit.viewportResults = result!.observations.map(({ viewport, status, elapsedMs, error, timings, captureCounts }) =>
            ({ viewport, status, elapsedMs, error, timings, captureCounts }));
          return timer.measure("checkpointMs", task.checkpoint);
        };
        await progress();
        // Checkpoints inside responsive capture are included in this high-level stage;
        // subtract them so visit timings do not count the same interval twice.
        const checkpointBefore = timer.ms.checkpointMs ?? 0;
        try {
          await timer.measure("responsiveCaptureMs", () => capturePageObservations(session.page, state.config, stagedPage.captureRoot, result!, progress));
        } finally {
          timer.ms.responsiveCaptureMs -= (timer.ms.checkpointMs ?? 0) - checkpointBefore;
        }
        await timer.measure("identityValidationMs", () => verifyResponsiveIdentity(task, result!, stagedPage.captureRoot));
        await progress();
        if (result.status !== "captured") {
          const missing = result.observations.filter(o => o.status !== "captured").map(o => `${o.viewport}: ${o.error || o.status}`);
          throw new Error(`Incomplete responsive capture: ${missing.join("; ")}`);
        }
        if (!state.config.exportOnly)
          await writeJson(path.join(stagedPage.captureRoot, "pages", result.id, "network.json"), session.network);
        committed = await timer.measure("publicationMs", () => commitPage(task, result!, stagedPage.captureRoot, target));
        visit.pageId = committed.id;
        if (previousIdentity && previousIdentity !== actual) existing.delete(previousIdentity);
        existing.set(actual, committed);
        if (current) images.set(result.id, { pixels: current, kind: "initial_memory" });
        visit.status = "accepted";
      }
    }
  } catch (error) {
    visit.status = task.signal?.aborted ? "interrupted" : "failed";
    visit.error = error instanceof Error ? error.message : String(error);
  } finally {
    if (!visit.finalUrl && !session.page.isClosed() && session.page.url() !== "about:blank")
      visit.finalUrl = session.page.url();
    await timer.measure("cleanupMs", () => fs.rm(path.dirname(path.join(state.root, staged)), { recursive: true, force: true }));
  }
  visit.elapsedMs = Math.round(performance.now() - started);
  visit.timings = timer.snapshot();
  if (committed) committed.elapsedMs = visit.elapsedMs;
  await task.checkpoint();
  console.log(JSON.stringify({ type: "url_visit", source: task.source.id, url: candidate.url,
    finalUrl: visit.finalUrl,
    status: visit.status, pageId: visit.pageId, similarity: visit.similarity,
    elapsedMs: visit.elapsedMs, timings: visit.timings, error: visit.error }));
  return visit;
}
