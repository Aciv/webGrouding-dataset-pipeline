#!/usr/bin/env node
/** Paired real-page measurements reuse one loaded page; no screenshot quality or label policy changes. */
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { extractDom } from "../../dist/browser/dom.js";
import { extractDomReference } from "../../dist/tests/fixtures/dom-extraction-v1.js";
import { prepareUrlCapture } from "../../dist/browser/online-preparation.js";

const flags = new Map();
for (let index = 2; index < process.argv.length; index += 2) {
  const key = process.argv[index], value = process.argv[index + 1];
  if (!["--output", "--sources", "--sites", "--repeats"].includes(key) || !value)
    throw new Error("Usage: dom-extraction.mjs --output NEW_DIR [--sources batch.json] [--sites 4] [--repeats 3]");
  flags.set(key, value);
}
if (!flags.has("--output")) throw new Error("--output is required");
const sites = Number(flags.get("--sites") ?? 4), repeats = Number(flags.get("--repeats") ?? 3);
if (![sites, repeats].every(value => Number.isInteger(value) && value > 0)) throw new Error("Invalid sites/repeats");
const project = path.resolve(import.meta.dirname, "../..");
const output = path.resolve(flags.get("--output"));
await fs.mkdir(path.dirname(output), { recursive: true });
await fs.mkdir(output); // Never overwrite prior evidence.
const input = JSON.parse(await fs.readFile(flags.get("--sources") ?? path.join(project, "examples/real-url-10.json"), "utf8"));
const report = { viewport: { width: 1440, height: 900 }, repeats, maxNodes: 20000, captureHeight: 8000,
  note: "Alternating old/new calls on the same live page; wall time includes browser work and transport. Not end-to-end throughput or live output parity.", sites: [] };
const browser = await chromium.launch({ headless: true });
try {
  for (const source of input.sources.slice(0, sites)) {
    const context = await browser.newContext({ viewport: report.viewport, deviceScaleFactor: 1,
      locale: "en-US", timezoneId: "UTC", colorScheme: "light" });
    const result = { id: source.id, url: source.baseUrl, calls: [] };
    try {
      const page = await context.newPage();
      const response = await page.goto(source.baseUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
      if (response && response.status() >= 400) throw new Error(`Navigation HTTP ${response.status()}`);
      await prepareUrlCapture(page, report.captureHeight, 300);
      for (let repeat = 0; repeat < repeats; repeat++) {
        const order = repeat % 2 ? ["cached", "reference"] : ["reference", "cached"];
        for (const method of order) {
          const start = performance.now();
          const snapshot = await (method === "cached" ? extractDom : extractDomReference)(page, report.maxNodes, report.captureHeight);
          result.calls.push({ repeat, method, elapsedMs: Math.round((performance.now() - start) * 10) / 10,
            nodes: snapshot.nodes.length, url: snapshot.url, documentId: snapshot.documentId });
        }
      }
      const means = Object.fromEntries(["reference", "cached"].map(method => {
        const calls = result.calls.filter(call => call.method === method);
        return [method, calls.reduce((total, call) => total + call.elapsedMs, 0) / calls.length];
      }));
      result.meanMs = means;
      result.speedup = means.reference / means.cached;
      result.status = "completed";
    } catch (error) {
      result.status = "failed";
      result.error = String(error);
    } finally {
      await context.close();
    }
    report.sites.push(result);
    await fs.writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(result));
  }
} finally {
  await browser.close();
}
