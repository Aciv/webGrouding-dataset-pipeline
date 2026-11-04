import fs from "node:fs/promises";
import path from "node:path";
import type { ArtifactConfig, Config } from "../domain/types.js";
import { resolveDiscovery } from "./discovery.js";

/** Read and validate batch JSON before opening a browser; resolve static roots here. */
export async function loadConfig(file: string): Promise<Config> {
  const value = JSON.parse(await fs.readFile(file, "utf8")) as Config;
  value.concurrency ??= 2;
  value.navigationTimeoutMs ??= 30000;
  value.settleMs ??= 300;
  value.maxNodes ??= 20000;
  value.maxHeight ??= 8000;
  if (value.exportOnly !== undefined && typeof value.exportOnly !== "boolean")
    throw new Error("exportOnly must be boolean");
  if (value.captureMode !== undefined && !["initial", "full"].includes(value.captureMode))
    throw new Error("captureMode must be initial or full");
  const identifier = /^[a-zA-Z0-9][a-zA-Z0-9_-]*$/;
  if (!Array.isArray(value.sources) || !value.sources.length ||
      !Array.isArray(value.pages) || !value.pages.length ||
      !Array.isArray(value.viewports) || !value.viewports.length)
    throw new Error("sources, pages and viewports must be non-empty arrays");
  for (const name of ["concurrency", "navigationTimeoutMs", "maxNodes", "maxHeight"] as const)
    if (!Number.isInteger(value[name]) || value[name] < 1)
      throw new Error(`Invalid ${name}`);
  if (!Number.isFinite(value.settleMs) || value.settleMs < 0)
    throw new Error("Invalid settleMs");
  for (const items of [value.sources, value.pages, value.viewports]) {
    if (new Set(items.map(item => item.id)).size !== items.length ||
        items.some(item => !identifier.test(item.id)))
      throw new Error("IDs must be unique within each list and safe directory names");
  }
  for (const viewport of value.viewports)
    if (![viewport.width, viewport.height].every(n => Number.isInteger(n) && n > 0))
      throw new Error(`Invalid viewport: ${viewport.id}`);
  for (const source of value.sources) {
    if (source.pathPrefix !== undefined && (typeof source.pathPrefix !== "string" || !source.pathPrefix.startsWith("/")))
      throw new Error(`Invalid source.pathPrefix: ${source.id}`);
    if (source.kind === "static" && source.root) {
      source.root = await fs.realpath(path.resolve(path.dirname(file), source.root));
      if (!(await fs.stat(source.root)).isDirectory()) throw new Error("Source root is not a directory");
    } else if (source.kind === "url" && source.baseUrl &&
        /^https?:$/.test(new URL(source.baseUrl).protocol)) { /* URL validated. */ }
    else throw new Error(`Invalid source: ${source.id}`);
  }
  for (const page of value.pages)
    if (!value.sources.some(source => source.id === page.source) || typeof page.path !== "string")
      throw new Error(`Invalid page source/path: ${page.id}`);
  if (value.artifacts !== undefined) {
    if (!value.artifacts || typeof value.artifacts !== "object" || Array.isArray(value.artifacts))
      throw new Error("artifacts must be an object");
    if (Object.keys(value.artifacts).some(key => !["initial", "overlayPng", "overlaySvg"].includes(key)))
      throw new Error("Unknown artifacts setting");
    value.artifacts = { initial: true, overlayPng: true, overlaySvg: true, ...(value.artifacts as Partial<ArtifactConfig>) };
    if (Object.values(value.artifacts).some(item => typeof item !== "boolean")) throw new Error("Artifact flags must be boolean");
  }
  if (value.discovery !== undefined) {
    if (!value.discovery || typeof value.discovery !== "object" || Array.isArray(value.discovery))
      throw new Error("discovery must be an object");
    value.discovery = resolveDiscovery(value.discovery, value);
  }
  if (value.captureMode === "initial" && value.artifacts?.initial === false)
    throw new Error("Initial capture mode requires artifacts.initial=true");
  return value;
}
