import fs from "node:fs/promises";
import path from "node:path";
import type { Config, PageResult, RawSnapshot } from "../domain/types.js";
import { writeJson } from "../infrastructure/files.js";
import { projectVisibleNodes } from "../domain/visibility.js";
import type { VisualHierarchy } from "../domain/hierarchy/types.js";
import { checkHierarchy, checkAnnotations, type BboxList } from "./annotation-checks.js";
import { pngDimensions } from "../infrastructure/png.js";
import { readJson } from "../infrastructure/files.js";

/** Audit persisted evidence without launching pages; writes a reproducible raw-data summary. */
export async function auditBatch(root: string) {
  const index = JSON.parse(await fs.readFile(path.join(root, "batch-index.json"), "utf8")) as {
    status: string; startedAt: string; updatedAt: string; config: Config; pages: PageResult[];
  };
  const errors: string[] = [];
  const tags: Record<string, number> = {};
  const sources: Record<string, number> = {};
  const networkIssues: { page: string; issues: unknown[] }[] = [];
  let observations = 0, nodes = 0, visibleCandidates = 0, stable = 0, truncated = 0;
  let artifactBytes = 0;
  for (const page of index.pages) {
    const sizes = new Set(page.observations.map(observation => observation.viewport));
    if (sizes.size !== page.observations.length) errors.push(`${page.id}: duplicate viewport observations`);
    if (page.status === "captured" && !index.config.viewports.every(size =>
      page.observations.some(observation => observation.viewport === size.id && observation.status === "captured")))
      errors.push(`${page.id}: captured page is missing configured viewports`);
    sources[page.source] = (sources[page.source] ?? 0) + 1;
    const documentIds = new Set<string>();
    const networkFile = path.join(root, "pages", page.id, "network.json");
    try {
      const issues: unknown[] = JSON.parse(await fs.readFile(networkFile, "utf8"));
      if (issues.length) networkIssues.push({ page: page.id, issues });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    for (const observation of page.observations) {
      if (observation.status !== "captured") continue;
      observations++;
      const directory = path.join(root, observation.directory);
      try {
        if (index.config.exportOnly) {
          const hierarchyFile = path.join(directory, "hierarchy.json");
          const hierarchy = await readJson<VisualHierarchy>(hierarchyFile);
          const expected = index.config.viewports.find(size => size.id === observation.viewport);
          const identity = observation.captureIdentity;
          if (!identity || !expected || identity.viewport.width !== expected.width || identity.viewport.height !== expected.height ||
              hierarchy.capture.documentId !== identity.documentId || hierarchy.capture.url !== identity.url)
            errors.push(`${page.id}/${observation.viewport}: compact capture identity mismatch`);
          const imageName = index.config.captureMode === "initial" ? "initial.png" : "full.png";
          if (hierarchy.capture.image !== imageName) throw new Error("Unexpected canonical image name");
          const image = await fs.readFile(path.join(directory, imageName));
          const size = pngDimensions(image);
          if (!expected || size.width !== expected.width || size.width !== hierarchy.capture.width || size.height !== hierarchy.capture.height ||
              (index.config.captureMode === "initial" ? size.height !== expected.height : size.height > index.config.maxHeight))
            errors.push(`${page.id}/${observation.viewport}: compact screenshot dimensions mismatch`);
          errors.push(...checkHierarchy(hierarchy).map(error => `${page.id}/${observation.viewport}: ${error}`));
          documentIds.add(hierarchy.capture.documentId);
          artifactBytes += image.length + (await fs.stat(hierarchyFile)).size;
          nodes += observation.nodes ?? 0;
          visibleCandidates += observation.visibleCandidates ?? 0;
          stable += Number(observation.stable);
          truncated += Number((observation.quality?.capture as { truncated?: boolean } | undefined)?.truncated);
          continue;
        }
        const files = ["nodes.json", "quality.json", "bboxes.json", "hierarchy.json", "dom.html"];
        if (index.config.artifacts?.initial !== false) files.push("initial.png");
        if (index.config.artifacts?.overlaySvg !== false) files.push("overlay.svg");
        if (index.config.artifacts?.overlayPng !== false) files.push("overlay.png");
        for (const name of files)
          artifactBytes += (await fs.stat(path.join(directory, name))).size;
        const snapshot: RawSnapshot = JSON.parse(await fs.readFile(path.join(directory, "nodes.json"), "utf8"));
        const expected = index.config.viewports.find(size => size.id === observation.viewport);
        if (index.config.captureMode === "initial" &&
            (snapshot.capture?.image !== "initial.png" || snapshot.capture.height !== snapshot.viewport.height))
          errors.push(`${page.id}/${observation.viewport}: initial capture range mismatch`);
        if (!expected || snapshot.viewport.width !== expected.width || snapshot.viewport.height !== expected.height)
          errors.push(`${page.id}/${observation.viewport}: configured viewport mismatch`);
        const boxes = await readJson<BboxList>(path.join(directory, "bboxes.json"));
        const hierarchy = await readJson<VisualHierarchy>(path.join(directory, "hierarchy.json"));
        errors.push(...checkAnnotations(snapshot, boxes, hierarchy).map(error => `${page.id}/${observation.viewport}: ${error}`));
        if (!snapshot.capture && index.config.artifacts?.initial === false)
          errors.push(`${page.id}/${observation.viewport}: no full capture while initial PNG is disabled`);
        documentIds.add(snapshot.documentId);
        const nodeIds = new Set(snapshot.nodes.map(node => node.nodeId));
        if (nodeIds.size !== snapshot.nodes.length) errors.push(`${page.id}/${observation.viewport}: duplicate IDs`);
        for (const node of snapshot.nodes) {
          tags[node.tag] = (tags[node.tag] ?? 0) + 1;
          if (node.parentId && !nodeIds.has(node.parentId))
            errors.push(`${page.id}/${observation.viewport}: dangling parent ${node.parentId}`);
        }
        if (index.config.artifacts?.initial !== false) {
          const image = await fs.readFile(path.join(directory, "initial.png"));
          const size = pngDimensions(image);
          if (size.width !== snapshot.viewport.width || size.height !== snapshot.viewport.height)
            errors.push(`${page.id}/${observation.viewport}: screenshot size mismatch`);
        }
        if (snapshot.capture) {
          const full = await fs.readFile(path.join(directory, snapshot.capture.image));
          if (snapshot.capture.image !== "initial.png" || index.config.artifacts?.initial === false)
            artifactBytes += full.length;
          const images = [full];
          if (index.config.artifacts?.overlayPng !== false) images.push(await fs.readFile(path.join(directory, "overlay.png")));
          for (const png of images) {
            const size = pngDimensions(png);
            if (size.width !== snapshot.capture.width || size.height !== snapshot.capture.height)
              errors.push(`${page.id}/${observation.viewport}: full image/overlay size mismatch`);
          }
          if (index.config.captureMode !== "initial" && snapshot.capture.height > index.config.maxHeight)
            errors.push(`${page.id}/${observation.viewport}: capture exceeds maxHeight`);
        }
        nodes += snapshot.nodes.length;
        visibleCandidates += projectVisibleNodes(snapshot).nodes.length;
        stable += Number(observation.stable);
        truncated += Number(snapshot.truncated);
      } catch (error) {
        errors.push(`${page.id}/${observation.viewport}: ${String(error)}`);
      }
    }
    if (documentIds.size > 1) errors.push(`${page.id}: document changed between viewports`);
  }
  const summary = {
    schema: "raw_dom_audit_v1", checkedAt: new Date().toISOString(),
    annotationStatus: "unlabeled_candidates", status: index.status,
    pages: index.pages.length, capturedPages: index.pages.filter(page => page.status === "captured").length,
    observations, nodes, visibleCandidates, stableObservations: stable, truncatedObservations: truncated,
    wallMs: Date.parse(index.updatedAt) - Date.parse(index.startedAt), artifactBytes,
    sources, tags, networkIssues, integrityErrors: errors,
    ...(index.config.exportOnly ? { auditScope: "exported_geometry_labels_and_capture_identity_without_raw_dom" } : {}),
    captureWarnings: index.pages.flatMap(page => page.observations.flatMap(observation =>
      observation.warnings?.length ? [{ page: page.id, viewport: observation.viewport, warnings: observation.warnings }] : [])),
  };
  await writeJson(path.join(root, "summary.json"), summary);
  return summary;
}
