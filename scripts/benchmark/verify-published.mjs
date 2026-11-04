#!/usr/bin/env node
/** Offline verification of published Curlie pairs; no browser or changes to capture/label rules. */
import fs from "node:fs/promises";
import path from "node:path";
import { inflateSync } from "node:zlib";
import { pngDimensions } from "../../dist/infrastructure/png.js";
import { checkHierarchy } from "../../dist/reporting/annotation-checks.js";

const root = path.resolve(process.argv[2] ?? "");
if (!process.argv[2]) throw new Error("verify-published.mjs RUN_DIRECTORY");
const state = JSON.parse(await fs.readFile(path.join(root, "collection-index.json"), "utf8"));
const sizes = state.settings.template.viewports;
const pages = state.batches.flatMap(batch => batch.pages ?? []);
const report = { checkedAt: new Date().toISOString(), status: state.status, pages: pages.length,
  viewports: 0, pngDecoded: 0, labels: 0, errors: [], warnings: [] };
const ids = new Set();

/** Decode Chromium PNG scanlines with built-in zlib, checking bounds, checksum and byte count. */
function verifyPng(buffer) {
  const { width, height } = pngDimensions(buffer);
  const bits = buffer[24], color = buffer[25], interlace = buffer[28];
  const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[color];
  if (!channels || interlace !== 0) throw new Error("Unsupported screenshot PNG pixel layout");
  const chunks = [];
  let offset = 8, ended = false;
  while (offset + 12 <= buffer.length) {
    const length = buffer.readUInt32BE(offset), kind = buffer.toString("ascii", offset + 4, offset + 8);
    if (offset + 12 + length > buffer.length) throw new Error("Truncated PNG chunk");
    if (kind === "IDAT") chunks.push(buffer.subarray(offset + 8, offset + 8 + length));
    offset += length + 12;
    if (kind === "IEND") { ended = true; break; }
  }
  if (!ended || !chunks.length) throw new Error("Incomplete PNG stream");
  const rowBytes = Math.ceil(width * channels * bits / 8), expected = (rowBytes + 1) * height;
  const raw = inflateSync(Buffer.concat(chunks), { maxOutputLength: expected + 1 });
  if (raw.length !== expected) throw new Error("PNG decoded scanline size mismatch");
  for (let y = 0; y < height; y++)
    if (raw[y * (rowBytes + 1)] > 4) throw new Error("Invalid PNG row filter");
  return { width, height };
}

/** Restrict metadata references to this run, without trusting arbitrary relative paths. */
function artifact(relative) {
  const absolute = path.resolve(root, relative);
  if (!absolute.startsWith(root + path.sep)) throw new Error("Artifact escapes run directory");
  return absolute;
}

for (const page of pages) {
  if (ids.has(page.id)) report.errors.push(`${page.id}: duplicate page ID`);
  ids.add(page.id);
  const documents = new Set();
  for (const viewport of sizes) {
    const label = `${page.id}/${viewport.id}`;
    try {
      const files = page.viewports[viewport.id];
      if (!files) throw new Error("Missing configured viewport");
      const imageFile = artifact(files.image), hierarchyFile = artifact(files.hierarchy);
      const hierarchy = JSON.parse(await fs.readFile(hierarchyFile, "utf8"));
      const image = verifyPng(await fs.readFile(imageFile));
      report.pngDecoded++;
      const initial = state.settings.template.captureMode === "initial";
      if (image.width !== viewport.width || image.width !== hierarchy.capture.width || image.height !== hierarchy.capture.height ||
          (initial ? image.height !== viewport.height : image.height > state.settings.template.maxHeight))
        throw new Error("Screenshot/config/label dimensions differ");
      if (hierarchy.capture.image !== path.basename(imageFile) || hierarchy.capture.url !== page.url)
        throw new Error("Screenshot URL/name does not match labels and index");
      const metadata = page.capture_metadata?.[viewport.id];
      if (!metadata?.captureIdentity || metadata.captureIdentity.url !== page.url ||
          metadata.captureIdentity.documentId !== hierarchy.capture.documentId ||
          metadata.captureIdentity.viewport.width !== viewport.width || metadata.captureIdentity.viewport.height !== viewport.height)
        throw new Error("Viewport/source identity mismatch");
      const expectedFiles = [path.basename(imageFile), path.basename(hierarchyFile)].sort();
      const actualFiles = (await fs.readdir(path.dirname(imageFile))).sort();
      if (JSON.stringify(actualFiles) !== JSON.stringify(expectedFiles)) throw new Error("Viewport directory is not a two-file training pair");
      report.errors.push(...checkHierarchy(hierarchy).map(error => `${label}: ${error}`));
      documents.add(hierarchy.capture.documentId);
      if (metadata.stable === false || metadata.warnings?.length)
        report.warnings.push({ viewport: label, stable: metadata.stable, warnings: metadata.warnings });
      report.labels += hierarchy.nodes.length;
      report.viewports++;
    } catch (error) {
      report.errors.push(`${label}: ${String(error)}`);
    }
  }
  if (documents.size > 1) report.errors.push(`${page.id}: document changed across viewports`);
}
if (state.totals.pages !== pages.length || state.totals.images !== report.viewports)
  report.errors.push("Index totals disagree with actual complete artifacts");
await fs.writeFile(path.join(root, "published-verification.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report));
if (report.errors.length) process.exitCode = 1;
