import type { Config, PageResult } from "../domain/types.js";

/** Shared manifest metadata for explicit-page and homepage-discovery workflows.
 * Callers clone this payload at checkpoint time and serialize their own writes.
 */
export function createBatchManifest(input: {
  config: Config;
  pages: PageResult[];
  status: string;
  startedAt: string;
  updatedAt?: string;
  browserVersion?: string;
  siteCounts?: Record<string, number>;
}) {
  const counts = Object.fromEntries(["pending", "running", "captured", "partial", "failed"]
    .map(status => [status, input.pages.filter(page => page.status === status).length]));
  return {
    schema: "raw_dom_batch_v1", status: input.status, startedAt: input.startedAt, updatedAt: input.updatedAt ?? new Date().toISOString(),
    browserVersion: input.browserVersion, playwrightVersion: "1.63.0", collectorVersion: "0.1.0", nodeVersion: process.version,
    annotationStatus: "unlabeled_candidates", config: input.config, counts,
    ...(input.siteCounts ? { siteCounts: input.siteCounts } : {}), pages: input.pages,
  };
}
