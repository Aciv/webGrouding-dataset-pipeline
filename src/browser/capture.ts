import fs from "node:fs/promises";
import path from "node:path";
import type { Page } from "playwright";
import type { Config, RawSnapshot, Viewport, ObservationResult } from "../domain/types.js";
import { StageTimer } from "../infrastructure/timing.js";
import { extractDom } from "./dom.js";
import { primeFullPage } from "./full-page.js";
import { writeJson } from "../infrastructure/files.js";
import { writeOverlay } from "../review/overlay.js";
import { writeBboxes } from "./bboxes.js";
import { writeHierarchy } from "../reporting/hierarchy-files.js";
import { prepareUrlCapture, urlScreenshot } from "./online-preparation.js";
import { prepareInitialCapture } from "./initial-preparation.js";
import { removeCapturePopups } from "./popup-cleanup.js";

/** Check node identity and border boxes around the screenshot; no semantic matching. */
export function geometryStable(before: RawSnapshot, after: RawSnapshot): boolean {
  if (before.documentId !== after.documentId || before.url !== after.url ||
      before.nodes.length !== after.nodes.length || before.scroll.x !== after.scroll.x ||
      before.scroll.y !== after.scroll.y) return false;
  return before.nodes.every((node, index) => {
    const next = after.nodes[index];
    return node.nodeId === next.nodeId && node.ownText === next.ownText &&
      node.visibility.cssVisible === next.visibility.cssVisible &&
      (["x", "y", "width", "height"] as const).every(key =>
        Math.abs(node.bbox[key] - next.bbox[key]) <= 0.5);
  });
}

/** Record a viewport-only image or a height-capped full-page image with aligned DOM boxes.
 * Resize is sequential on the same page so WeakMap identity survives CSS reflow.
 * Stability/asset warnings are evidence for review, never claimed to be GT quality.
 */
export async function captureViewport(page: Page, viewport: Viewport, config: Config,
    root: string, relative: string, options: {
      onInitial?: (png: Buffer) => void;
      onSnapshot?: (snapshot: RawSnapshot) => void;
      preparation?: "strict" | "bounded_online";
    } = {}): Promise<ObservationResult> {
  const started = performance.now();
  const timer = new StageTimer();
  const counts = { attempts: 0, domExtractions: 0, initialScreenshots: 0, fullScreenshots: 0 };
  const directory = path.join(root, relative);
  const warnings: string[] = [];
  const initialOnly = config.captureMode === "initial";
  if (initialOnly && config.artifacts?.initial === false)
    throw new Error("Initial capture mode requires artifacts.initial=true");
  const needInitial = initialOnly || (!config.exportOnly && config.artifacts?.initial !== false) || Boolean(options.onInitial);
  const removedPopups = await timer.measure("setupMs", async () => {
    await fs.mkdir(directory, { recursive: true });
    await page.setViewportSize(viewport);
    const removed = await removeCapturePopups(page);
    await page.evaluate(() => scrollTo(0, 0));
    return removed;
  });

  // URL batches use the original online policy: warnings, fixed prepared range and bounded waits.
  // Local/static pages retain their strict preparation; screenshot/DOM dimensions stay mandatory.
  const { online, readiness } = await timer.measure("preparationMs", async () => {
    const online = initialOnly ? await prepareInitialCapture(page, viewport.height, config.settleMs)
      : options.preparation === "bounded_online" ? await prepareUrlCapture(page, config.maxHeight, config.settleMs) : null;
    const readiness = online?.images ?? await primeFullPage(page, config.settleMs, { maxHeight: config.maxHeight });
    await page.evaluate(() => scrollTo({ left: 0, top: 0, behavior: "instant" }));
    return { online, readiness };
  });
  if (online) warnings.push(...online.warnings);
  const screenshot = online
    ? (settings: Parameters<Page["screenshot"]>[0]) => urlScreenshot(page, settings)
    : (settings: Parameters<Page["screenshot"]>[0]) => page.screenshot(settings);
  let height = 0;

  let snapshot!: RawSnapshot;
  let stable = false;
  let image!: Buffer;
  let initial: Buffer | undefined;
  for (let attempt = 0; attempt < 2; attempt++) {
    counts.attempts++;
    height = await timer.measure("geometryMs", async () => {
      const latePopups = await removeCapturePopups(page);
      removedPopups.push(...latePopups);
      if (latePopups.length) await page.waitForTimeout(config.settleMs);
      return initialOnly ? viewport.height : page.evaluate(limit =>
        Math.min(Math.max(document.documentElement.scrollHeight, innerHeight), limit),
        Math.min(config.maxHeight, online?.capturedHeight ?? config.maxHeight));
    });
    if (height <= 0) throw new Error("Capture preparation produced an empty image range");
    counts.domExtractions++;
    snapshot = await timer.measure("domMs", () => extractDom(page, config.maxNodes, height));
    if (snapshot.scroll.x !== 0 || snapshot.scroll.y !== 0)
      throw new Error("DOM geometry must be measured at document origin");
    if (needInitial) {
      counts.initialScreenshots++;
      initial = await timer.measure("initialScreenshotMs", () => screenshot({ fullPage: false, scale: "css", timeout: 10000 }));
    }
    if (initialOnly) image = initial!;
    else {
      counts.fullScreenshots++;
      image = await timer.measure("fullScreenshotMs", () => screenshot({ fullPage: true,
        clip: { x: 0, y: 0, width: viewport.width, height }, scale: "css", timeout: 10000 }));
    }
    counts.domExtractions++;
    const after = await timer.measure("domMs", () => extractDom(page, config.maxNodes, height));
    stable = timer.measureSync("stabilityMs", () => geometryStable(snapshot, after) && image.readUInt32BE(20) === height);
    if (stable) break;
    if (attempt === 0) await timer.measure("stabilityMs", () => page.waitForTimeout(config.settleMs));
  }
  if ((initial && (initial.readUInt32BE(16) !== viewport.width || initial.readUInt32BE(20) !== viewport.height)) ||
      image.readUInt32BE(16) !== viewport.width || image.readUInt32BE(20) !== height)
    throw new Error(`Screenshot dimensions differ: initial=${initial ? `${initial.readUInt32BE(16)}x${initial.readUInt32BE(20)}` : "not_requested"}, ` +
      `full=${image.readUInt32BE(16)}x${image.readUInt32BE(20)}, expected=${viewport.width}x${height}`);
  if (!stable) warnings.push("Geometry/content changed around screenshot; review before labeling");
  if (snapshot.truncated) warnings.push(`DOM truncated at ${config.maxNodes} nodes`);
  snapshot.capture = { image: initialOnly ? "initial.png" : "full.png", width: viewport.width, height,
    maxHeight: initialOnly ? viewport.height : config.maxHeight, truncated: snapshot.document.height > height };
  if (initial) options.onInitial?.(initial);
  options.onSnapshot?.(snapshot);
  const imageWrites = async () => {
    if (initialOnly || !config.exportOnly && config.artifacts?.initial !== false)
      await fs.writeFile(path.join(directory, "initial.png"), initial!);
    if (!initialOnly) await fs.writeFile(path.join(directory, "full.png"), image);
  };
  await timer.measure("writeMs", imageWrites);
  if (!config.exportOnly) {
    await writeJson(path.join(directory, "nodes.json"), snapshot, timer);
    const html = await timer.measure("metadataMs", () => page.content());
    await timer.measure("writeMs", () => fs.writeFile(path.join(directory, "dom.html"), html));
    await timer.measure("diagnosticsMs", async () => {
      if (config.artifacts?.overlaySvg !== false) await writeOverlay(directory, snapshot);
      await writeBboxes(page, directory, snapshot, image, { png: config.artifacts?.overlayPng });
    });
  }
  const hierarchy = await writeHierarchy(directory, snapshot, { diagnostics: !config.exportOnly, timer });
  const images = await timer.measure("metadataMs", () => page.evaluate(height => [...document.images].flatMap(img => {
    const rect = img.getBoundingClientRect();
    if (rect.bottom <= 0 || rect.top >= height || rect.right <= 0 || rect.left >= innerWidth)
      return [];
    return [{ url: img.currentSrc || img.src, complete: img.complete,
      decoded: img.complete && img.naturalWidth > 0 }];
  }), height));
  if (images.some(image => !image.decoded)) warnings.push("Some in-range images are pending or broken");
  const fontsStatus = await timer.measure("metadataMs", () => page.evaluate(() => document.fonts.status));
  const quality = { stable, warnings, images, readiness,
    preparation: online ?? { method: "strict_full_page" },
    capture: snapshot.capture,
    fontsStatus,
    annotationStatus: "unlabeled_candidates", screenshotPolicy: "native_scrollbars_no_injected_css",
    popupPolicy: "remove_popups_and_content_covering_bottom_floats_v2", removedPopups,
    animationPolicy: "unchanged", screenshotAndDomAtomic: false,
    limitations: ["No occlusion mask", "No iframe or shadow-root traversal", "No semantic identity inference"] };
  if (!config.exportOnly) await writeJson(path.join(directory, "quality.json"), quality, timer);
  const timings = timer.snapshot();
  const elapsedMs = Math.round(performance.now() - started);
  timings.otherMs = Math.max(0, Math.round((elapsedMs - Object.values(timings).reduce((a, b) => a + b, 0)) * 10) / 10);
  timings.totalMs = elapsedMs;
  console.log(JSON.stringify({ type: "capture_timing", directory: relative, viewport: viewport.id, timings, counts }));
  return {
    viewport: viewport.id, status: "captured", directory: relative,
    elapsedMs, timings, captureCounts: counts,
    captureIdentity: { url: snapshot.url, documentId: snapshot.documentId, viewport: snapshot.viewport },
    nodes: snapshot.nodes.length,
    visibleCandidates: hierarchy.stats.visible_after_clipping,
    ...(config.exportOnly ? { quality } : {}),
    stable, warnings,
  };
}
