import fs from "node:fs/promises";
import path from "node:path";
import type { ObservationResult, PageResult, RawSnapshot } from "../../domain/types.js";
import type { DiscoveryVisit } from "../../domain/discovery/types.js";
import { pageIdentity, withinScope } from "../../domain/discovery/url-policy.js";
import { readJson } from "../../infrastructure/files.js";
import type { SiteTask } from "./types.js";
import type { VisualHierarchy } from "../../domain/hierarchy/types.js";

/** Reserve before the first await, including configured entry IDs belonging to other source workers. */
export function allocatePageId(task: SiteTask, actualUrl: string): string {
  const { state, seed, site } = task;
  if (pageIdentity(actualUrl) === pageIdentity(site.entryUrl) && !state.pages.some(p => p.id === seed.id)) return seed.id;
  let number = 1, id: string;
  do { id = `${seed.id}-p${String(number++).padStart(3, "0")}`; } while (state.reservedIds.has(id));
  state.reservedIds.add(id);
  return id;
}

/** Validate staged identity before any frame is accepted or copied to its permanent page directory. */
export async function validateReference(task: SiteTask, stagedDirectory: string, expected: string, liveUrl: string,
    snapshot?: RawSnapshot): Promise<RawSnapshot> {
  const raw = snapshot ?? await readJson<RawSnapshot>(path.join(task.state.root, stagedDirectory, "nodes.json"));
  if (pageIdentity(raw.url) !== expected || pageIdentity(liveUrl) !== expected ||
      !withinScope(task.site.entryUrl, task.source.pathPrefix ?? "/", raw.url))
    throw new Error("URL changed during reference capture");
  return raw;
}

/** Stage a reference for the remaining sizes; no page or quota is published at this point. */
export async function stageReference(task: SiteTask, visit: DiscoveryVisit, actualUrl: string,
    staged: string, first: ObservationResult, raw: RawSnapshot, target?: PageResult): Promise<{ result: PageResult; captureRoot: string }> {
  const { state, source } = task;
  const id = target?.id ?? allocatePageId(task, actualUrl);
  const captureRoot = path.dirname(path.join(state.root, staged));
  const directory = `pages/${id}/${state.reference.id}`;
  await fs.mkdir(path.dirname(path.join(captureRoot, directory)), { recursive: true });
  await fs.rename(path.join(state.root, staged), path.join(captureRoot, directory));
  const url = new URL(actualUrl);
  const result: PageResult = { id, source: source.id, url: url.href, urlIdentity: pageIdentity(url.href), title: raw.title,
    path: url.pathname + url.search, status: "running", observations: [{ ...first, directory }],
    discoveredFrom: visit.candidate.fromUrl, discoveredAt: visit.startedAt };
  visit.candidatePageId = id; visit.status = "capturing";
  return { result, captureRoot };
}

/** Admit only complete responsive bundles. Repairs preserve human files and mutate old records only after copying succeeds. */
export async function commitPage(task: SiteTask, result: PageResult, captureRoot: string, target?: PageResult): Promise<PageResult> {
  if (result.status !== "captured") throw new Error("Cannot commit incomplete responsive page");
  await fs.mkdir(path.join(task.state.root, "pages", result.id), { recursive: true });
  await fs.cp(path.join(captureRoot, "pages", result.id), path.join(task.state.root, "pages", result.id), { recursive: true });
  const committed = target ?? result;
  if (target) { Object.assign(target, result); delete target.error; }
  else task.state.pages.push(committed);
  if (!task.site.acceptedPageIds.includes(result.id)) task.site.acceptedPageIds.push(result.id);
  return committed;
}

/** Recheck saved frames rather than assuming viewport changes cannot trigger URL redirects. */
export async function verifyResponsiveIdentity(task: SiteTask, result: PageResult, captureRoot = task.state.root): Promise<void> {
  for (const observation of result.observations.filter(o => o.status === "captured")) {
    const raw = task.state.config.exportOnly
      ? (await readJson<VisualHierarchy>(path.join(captureRoot, observation.directory, "hierarchy.json"))).capture
      : await readJson<RawSnapshot>(path.join(captureRoot, observation.directory, "nodes.json"));
    if (!raw.url) throw new Error("Captured URL metadata is missing");
    if (pageIdentity(raw.url) !== result.urlIdentity || !withinScope(task.site.entryUrl, task.source.pathPrefix ?? "/", raw.url)) {
      observation.status = "failed";
      observation.error = "URL changed during responsive capture";
    }
  }
  const successful = new Set(result.observations.filter(o => o.status === "captured").map(o => o.viewport));
  result.status = task.state.config.viewports.every(v => successful.has(v.id)) ? "captured" : "partial";
}
