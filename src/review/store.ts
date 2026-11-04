import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import type { PageResult, RawSnapshot } from "../domain/types.js";
import { writeJson } from "../infrastructure/files.js";
import type { CaptureReference, HumanReview, ReviewBox, ReviewIssue } from "./types.js";
import { projectVisibleNodes } from "../domain/visibility.js";
import { buildVisualHierarchy } from "../domain/hierarchy/normalize.js";

/** Controlled HTTP error for invalid requests or stale annotations. */
export class ReviewError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

/** Missing files mean unreviewed; corrupt records must not silently become empty. */
async function readOptional(file: string): Promise<HumanReview | undefined> {
  try { return JSON.parse(await fs.readFile(file, "utf8")) as HumanReview; }
  catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined; throw error; }
}

/** Compare the captured evidence, rather than its mutable path. */
function sameCapture(a: CaptureReference, b: CaptureReference): boolean {
  return a.documentId === b.documentId && a.capturedAt === b.capturedAt && a.image === b.image &&
    a.width === b.width && a.height === b.height;
}

/** Free-text labels remain editable; suggestions do not freeze the training taxonomy. */
function text(value: unknown, maximum: number): string {
  if (value === undefined || value === null) return "";
  if (typeof value !== "string" || value.length > maximum)
    throw new ReviewError(400, `文本格式不正确或超过 ${maximum} 字符`);
  return value.trim();
}

/** Require positive finite rectangles within the original, unscaled screenshot. */
function box(value: unknown, capture: CaptureReference): ReviewBox {
  if (!Array.isArray(value) || value.length !== 4 ||
      !value.every(item => typeof item === "number" && Number.isFinite(item)))
    throw new ReviewError(400, "bbox 必须为四个有限数字 [x,y,w,h]");
  const [x, y, width, height] = value as number[];
  if (x < 0 || y < 0 || width <= 0 || height <= 0 ||
      x + width > capture.width + 0.001 || y + height > capture.height + 0.001)
    throw new ReviewError(400, "bbox 超出原始截图范围");
  return [x, y, width, height];
}

/** File-backed reviews; serialized writes and revision checks avoid lost updates. */
export function createReviewStore(root: string) {
  let writes = Promise.resolve();

  /** Resolve only captured observations listed in the batch; reject escaping symlinks. */
  async function location(pageId: string, viewportId: string) {
    const index = JSON.parse(await fs.readFile(path.join(root, "batch-index.json"), "utf8")) as { pages: PageResult[] };
    const page = index.pages.find(item => item.id === pageId);
    const item = page?.observations.find(item => item.viewport === viewportId && item.status === "captured");
    if (!item) throw new ReviewError(404, "没有对应的已采集页面/分辨率");
    const directory = await fs.realpath(path.resolve(root, item.directory));
    if (!directory.startsWith(`${root}${path.sep}`)) throw new ReviewError(403, "观测目录超出数据集范围");
    const nodesFile = await fs.realpath(path.join(directory, "nodes.json"));
    if (!nodesFile.startsWith(`${directory}${path.sep}`)) throw new ReviewError(403, "节点文件超出观测范围");
    const file = path.join(directory, "human-review.json");
    const entry = await fs.lstat(file).catch(error => {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
      throw error;
    });
    if (entry?.isSymbolicLink()) throw new ReviewError(403, "不允许通过符号链接写入核验文件");
    return { directory, file, nodesFile };
  }

  /** Load geometry only for the active or previously reviewed observation. */
  async function observation(pageId: string, viewportId: string) {
    const resolved = await location(pageId, viewportId);
    const snapshot: RawSnapshot = JSON.parse(await fs.readFile(resolved.nodesFile, "utf8"));
    const capture: CaptureReference = {
      documentId: snapshot.documentId, capturedAt: snapshot.capturedAt,
      image: snapshot.capture?.image ?? "initial.png",
      width: snapshot.capture?.width ?? snapshot.viewport.width,
      height: snapshot.capture?.height ?? snapshot.viewport.height,
    };
    return { ...resolved, snapshot, capture };
  }

  /** Aggregate current-capture issues and tag counts for later labeling analysis. */
  async function summary() {
    const index = JSON.parse(await fs.readFile(path.join(root, "batch-index.json"), "utf8")) as { pages: PageResult[] };
    const observations = [];
    const byType: Record<string, number> = Object.create(null), byTag: Record<string, number> = Object.create(null);
    let existingBoxIssues = 0, missingBoxIssues = 0;
    for (const page of index.pages) for (const item of page.observations) {
      if (item.status !== "captured") continue;
      const resolved = await location(page.id, item.viewport);
      const review = await readOptional(resolved.file);
      const current = review ? await observation(page.id, item.viewport) : undefined;
      const stale = Boolean(review && current && !sameCapture(review.capture, current.capture));
      const issues = review && !stale ? review.issues : [];
      for (const issue of issues) {
        if (issue.kind === "existing_box") existingBoxIssues++; else missingBoxIssues++;
        byType[`${issue.kind}:${issue.issueType}`] = (byType[`${issue.kind}:${issue.issueType}`] ?? 0) + 1;
        if (issue.tag) byTag[issue.tag] = (byTag[issue.tag] ?? 0) + 1;
      }
      observations.push({ pageId: page.id, viewportId: item.viewport,
        reviewed: Boolean(review?.reviewed && !stale), issues: issues.length, stale,
        file: `${item.directory}/human-review.json` });
    }
    return { schema: "dom_bbox_review_summary_v1", updatedAt: new Date().toISOString(),
      observations, existingBoxIssues, missingBoxIssues, byType, byTag };
  }

  return {
    summary,
    /** Load feedback and explicitly flag records bound to an older capture. */
    async get(pageId: string, viewportId: string) {
      const resolved = await observation(pageId, viewportId);
      const review = await readOptional(resolved.file);
      return {
        review: review ?? {
          schema: "dom_bbox_human_review_v1", pageId, viewportId, capture: resolved.capture,
          coordinateSystem: "image_css_pixels", revision: 0, reviewed: false, issues: [], updatedAt: null,
        } satisfies HumanReview,
        currentCapture: resolved.capture,
        stale: Boolean(review && !sameCapture(review.capture, resolved.capture)),
      };
    },
    /** Validate feedback and replace atomically; older capture reviews are archived. */
    async save(pageId: string, viewportId: string, input: unknown): Promise<HumanReview> {
      const operation = writes.then(async () => {
        if (!input || typeof input !== "object") throw new ReviewError(400, "需要 JSON 对象");
        const body = input as Record<string, unknown>;
        const resolved = await observation(pageId, viewportId);
        const previous = await readOptional(resolved.file);
        if (body.revision !== (previous?.revision ?? 0))
          throw new ReviewError(409, "记录已被其他页面更新，请重新加载后再保存");
        if (!body.capture || !sameCapture(body.capture as CaptureReference, resolved.capture))
          throw new ReviewError(409, "截图已经重新采集，请重新加载核验页面");
        if (typeof body.reviewed !== "boolean" || !Array.isArray(body.issues) || body.issues.length > 1000)
          throw new ReviewError(400, "核验状态或问题列表无效（最多 1000 条）");
        const now = new Date().toISOString();
        const used = new Set<string>();
        const visible = new Map(projectVisibleNodes(resolved.snapshot).nodes.map(node => [node.nodeId, node]));
        const visual = new Map(buildVisualHierarchy(resolved.snapshot).nodes.map(node => [node.node_id, node]));
        const issues: ReviewIssue[] = body.issues.map(value => {
          if (!value || typeof value !== "object") throw new ReviewError(400, "问题格式错误");
          const item = value as Record<string, unknown>;
          const id = text(item.id, 100);
          if (!/^[a-zA-Z0-9_-]+$/.test(id) || used.has(id)) throw new ReviewError(400, "问题 ID 无效或重复");
          used.add(id);
          if (item.kind !== "existing_box" && item.kind !== "missing_box") throw new ReviewError(400, "未知问题种类");
          const issueType = text(item.issueType, 200), reason = text(item.reason, 4000);
          const content = text(item.content, 4000), expectedTag = text(item.expectedTag, 100);
          if (!issueType || (item.kind === "existing_box" && !reason) || (item.kind === "missing_box" && !content))
            throw new ReviewError(400, "请填写问题类型，以及错误原因或缺失内容");
          let bbox: ReviewBox;
          let evidence: Pick<ReviewIssue, "nodeId" | "tag" | "selector"> = {};
          const old = previous && sameCapture(previous.capture, resolved.capture)
            ? previous.issues.find(issue => issue.id === id) : undefined;
          if (item.kind === "existing_box") {
            const node = resolved.snapshot.nodes.find(node => node.nodeId === item.nodeId);
            const projected = visible.get(String(item.nodeId));
            const historical = old?.kind === "existing_box" && old.nodeId === item.nodeId;
            if (!node || !historical && !projected?.captureBbox)
              throw new ReviewError(400, "已有框必须对应当前截图中的可见 DOM 节点");
            const rect = projected?.captureBbox;
            // Preserve feedback about an old invisible box even after rules correctly remove it.
            const normalized = visual.get(node.nodeId)?.bbox;
            const normalizedSelected = normalized && Array.isArray(item.bbox) &&
              normalized.every((value, index) => item.bbox instanceof Array && item.bbox[index] === value);
            bbox = historical ? old!.bbox : normalizedSelected ? normalized! :
              [rect!.x, rect!.y, rect!.width, rect!.height];
            evidence = { nodeId: node.nodeId, tag: node.tag, selector: node.selector };
          } else bbox = box(item.bbox, resolved.capture);
          return { id, kind: item.kind, issueType, reason, content, expectedTag, bbox, ...evidence,
            createdAt: old?.createdAt ?? now, updatedAt: now };
        });
        const review: HumanReview = {
          schema: "dom_bbox_human_review_v1", pageId, viewportId, capture: resolved.capture,
          coordinateSystem: "image_css_pixels", revision: (previous?.revision ?? 0) + 1,
          reviewed: body.reviewed, issues, updatedAt: now,
        };
        if (previous && !sameCapture(previous.capture, resolved.capture)) {
          const history = path.join(resolved.directory, "human-review-history");
          const entry = await fs.lstat(history).catch(error => {
            if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
            throw error;
          });
          if (entry?.isSymbolicLink()) throw new ReviewError(403, "核验历史目录不允许是符号链接");
          await writeJson(path.join(history, `${randomUUID()}.json`), previous);
        }
        await writeJson(resolved.file, review);
        await writeJson(path.join(root, "human-review-summary.json"), await summary());
        return review;
      });
      writes = operation.then(() => {}, () => {});
      return operation;
    },
  };
}
