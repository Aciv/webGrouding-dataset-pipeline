import type { Config, DiscoveryConfig } from "../domain/types.js";
import { MAX_REFERENCE_PIXELS } from "../domain/discovery/image-similarity.js";

/** Resolve one bounded homepage inventory; there is deliberately no child BFS/depth option. */
export function resolveDiscovery(input: Partial<DiscoveryConfig>, config: Config): DiscoveryConfig {
  const allowed = ["enabled", "minPages", "maxPages", "maxVisits", "maxCandidates", "menuInteractions", "similarityThreshold", "viewport"];
  if (Object.keys(input).some(key => !allowed.includes(key))) throw new Error("Unknown discovery setting");
  const value: DiscoveryConfig = { enabled: false, minPages: 2, maxPages: 5, maxVisits: 20, maxCandidates: 60,
    menuInteractions: 4, similarityThreshold: 0.95,
    viewport: config.viewports.some(v => v.id === "desktop") ? "desktop" : config.viewports[0].id, ...input };
  if (typeof value.enabled !== "boolean") throw new Error("Invalid discovery.enabled");
  // Entry-only jobs use the existing atomic responsive publisher, with no search quota.
  if (!value.enabled) Object.assign(value, { minPages: 1, maxPages: 1, maxVisits: 1, menuInteractions: 0 });
  for (const key of ["minPages", "maxPages", "maxVisits", "maxCandidates"] as const)
    if (!Number.isInteger(value[key]) || value[key] < 1) throw new Error(`Invalid discovery.${key}`);
  if (value.minPages < (value.enabled ? 2 : 1) || value.maxPages > 5 || value.minPages > value.maxPages)
    throw new Error("discovery requires 2–5 pages when enabled, or one entry page when disabled");
  if (value.maxVisits < value.maxPages || value.maxCandidates < value.maxPages)
    throw new Error("discovery maxVisits/maxCandidates must cover maxPages");
  if (!Number.isInteger(value.menuInteractions) || value.menuInteractions < 0 ||
      !Number.isFinite(value.similarityThreshold) || value.similarityThreshold < 0 || value.similarityThreshold > 1)
    throw new Error("Invalid discovery menuInteractions or similarityThreshold");
  if (!config.viewports.some(v => v.id === value.viewport)) throw new Error("Unknown discovery.viewport");
  const reference = config.viewports.find(v => v.id === value.viewport)!;
  if (reference.width * reference.height > MAX_REFERENCE_PIXELS)
    throw new Error(`Discovery reference exceeds ${MAX_REFERENCE_PIXELS} pixel comparison budget`);
  if (config.captureMode !== "initial" && reference.height > config.maxHeight)
    throw new Error("Discovery reference height must fit within maxHeight");
  if (config.sources.some(s => s.kind !== "url")) throw new Error("Homepage discovery accepts URL sources only");
  for (const source of config.sources)
    if (config.pages.filter(p => p.source === source.id).length !== 1)
      throw new Error(`Homepage discovery requires one entry page per source: ${source.id}`);
  return value;
}
