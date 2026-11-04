import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium, type Page } from "playwright";
import { collect } from "../application/collect.js";
import { serveReview } from "../review/server.js";
import type { Config } from "../domain/types.js";
import type { HumanReview } from "../review/types.js";
import { writeHierarchy } from "../reporting/hierarchy-files.js";

/** Exercise real forms/drawing, disk recovery, validation and concurrent-editor conflicts. */
async function test(): Promise<void> {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "dom-review-"));
  const source = path.join(temporary, "site"), root = path.join(temporary, "output");
  await fs.mkdir(source);
  await fs.writeFile(path.join(source, "index.html"), `<!doctype html><html><title>Review fixture</title>
    <style>body{margin:0}main{padding:30px;height:1800px}button{padding:15px}</style>
    <body><main><button id="submit"><span>Save</span></button><p>Nested text</p>
    <a id="picture-link" href="#picture"><img width="100" height="100" alt="Fixture image"
      src="data:image/svg+xml;base64,${Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="blue"/></svg>').toString('base64')}"></a>
    </main></body></html>`);
  const config: Config = {
    viewports: [{ id: "mobile", width: 390, height: 844 }, { id: "desktop", width: 900, height: 600 }],
    sources: [{ id: "fixture", kind: "static", root: source }], pages: [{ id: "home", source: "fixture", path: "/" }],
    concurrency: 1, navigationTimeoutMs: 5000, settleMs: 10, maxNodes: 1000, maxHeight: 1400,
  };
  let server: Awaited<ReturnType<typeof serveReview>> | undefined;
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
  const reviewFile = path.join(root, "pages/home/mobile/human-review.json");
  const read = async (): Promise<HumanReview> => JSON.parse(await fs.readFile(reviewFile, "utf8"));
  /** Await a PUT completion and its UI update, rather than arbitrary browser sleeps. */
  async function save(page: Page) {
    const response = page.waitForResponse(response => response.url().includes("/api/review?") && response.request().method() === "PUT");
    await page.locator("#save-issue").click();
    assert.equal((await response).status(), 200);
    await page.waitForFunction(() => document.getElementById("status")?.textContent?.startsWith("已保存到"));
  }
  try {
    await collect(config, root);
    server = await serveReview(root, 0);
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(10000);
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(server.url);
    await page.locator("#svg rect").first().waitFor();
    await page.locator("#tag").selectOption("button");
    await page.locator("details:has(#nodes) > summary").click();
    await page.locator("#nodes button").first().click();
    await page.locator("#issue-type").fill("框过小");
    await page.locator("#reason").fill("需要完整按钮框，而非内部文字框");
    await page.locator("#expected-tag").fill("button");
    await save(page);
    const first = await read();
    assert.equal(first.issues.length, 1);
    assert.equal(first.issues[0].tag, "button");
    assert.equal(first.issues[0].selector, "#submit");
    assert.match(first.issues[0].reason, /完整按钮/);

    // Draw after zooming AND scrolling: persisted geometry must stay in original image pixels.
    await page.locator("#zoom").selectOption("0.5");
    await page.locator("#missing-mode").click();
    await page.locator("#workspace").evaluate(element => { element.scrollTop = 120; });
    const bounds = await page.locator("#svg").boundingBox();
    assert(bounds);
    const scale = bounds.width / 390;
    await page.mouse.move(bounds.x + 80 * scale, bounds.y + 550 * scale);
    await page.mouse.down();
    await page.mouse.move(bounds.x + 200 * scale, bounds.y + 640 * scale, { steps: 5 });
    await page.mouse.up();
    await page.locator("#issue-type").fill("缺失图标");
    await page.locator("#content").fill("未标注的图标区域");
    await save(page);
    const second = await read();
    assert.equal(second.issues.length, 2);
    const missing = second.issues.find(issue => issue.kind === "missing_box")!;
    assert.deepEqual(missing.bbox, [80, 550, 120, 90]);
    assert.equal(second.coordinateSystem, "image_css_pixels");
    assert.equal(missing.nodeId, undefined);
    const summary = JSON.parse(await fs.readFile(path.join(root, "human-review-summary.json"), "utf8"));
    assert.equal(summary.existingBoxIssues, 1);
    assert.equal(summary.missingBoxIssues, 1);
    assert.equal(summary.byTag.button, 1);

    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll('.issue-card').length === 2);
    await page.locator(".issue-card.missing").click();
    assert.equal(await page.locator("#content").inputValue(), "未标注的图标区域");
    await page.locator("#content").fill("修改后的缺失内容");
    await save(page);
    assert.equal((await read()).issues.find(issue => issue.id === missing.id)?.content, "修改后的缺失内容");
    page.once("dialog", dialog => dialog.accept());
    await page.locator(".issue-card.missing button").click();
    await page.waitForFunction(() => document.querySelectorAll('.issue-card').length === 1);
    assert.equal((await read()).issues.length, 1);
    await page.locator("#reviewed").click();
    await page.waitForFunction(() => document.getElementById('reviewed')?.textContent?.startsWith('已检查'));
    assert.equal((await read()).reviewed, true);

    await page.getByRole("button", { name: /^desktop · captured/ }).click();
    await page.waitForFunction(() => document.querySelector('#image')?.getAttribute('src')?.includes('/desktop/'));
    assert.equal(await page.locator(".issue-card").count(), 0, "Issues must stay scoped to their viewport");
    const current = await read();
    const endpoint = new URL("/api/review?page=home&viewport=mobile", server.url).href;
    const put = (body: unknown, extra: Record<string, string> = {}) => fetch(endpoint, {
      method: "PUT", headers: { "Content-Type": "application/json", ...extra }, body: JSON.stringify(body),
    });
    assert.equal((await put({ ...current, revision: 0 })).status, 409);
    assert.equal((await put({ ...current, issues: [{ id: "outside", kind: "missing_box", issueType: "其他", content: "越界", bbox: [380, 100, 100, 30] }] })).status, 400);
    assert.equal((await put(current, { Origin: "https://example.com" })).status, 403);
    assert.equal((await read()).revision, current.revision);

    // Recreate the service to prove reviews are file-backed, not just browser state.
    await server.close();
    server = await serveReview(root, 0);
    const restored = await fetch(new URL("/api/review?page=home&viewport=mobile", server.url)).then(response => response.json());
    assert.equal(restored.review.issues.length, 1);
    assert.equal(restored.review.reviewed, true);
    const nodesFile = path.join(root, "pages/home/mobile/nodes.json");
    const nodes = JSON.parse(await fs.readFile(nodesFile, "utf8"));
    nodes.capturedAt = new Date(Date.parse(nodes.capturedAt) + 1000).toISOString();
    await fs.writeFile(nodesFile, JSON.stringify(nodes));
    await writeHierarchy(path.dirname(nodesFile), nodes);
    await page.goto(new URL("?page=home&viewport=mobile", server.url).href);
    await page.locator("#restart-review").waitFor();
    assert.equal((await read()).issues.length, 1, "Opening stale feedback must not erase it");
    page.once("dialog", dialog => dialog.accept());
    await page.locator("#restart-review").click();
    await page.locator("#reviewed").click();
    await page.waitForFunction(() => document.getElementById('reviewed')?.textContent?.startsWith('已检查'));
    const archived = await fs.readdir(path.join(root, "pages/home/mobile/human-review-history"));
    assert.equal(archived.length, 1);
    const old = JSON.parse(await fs.readFile(path.join(root, "pages/home/mobile/human-review-history", archived[0]), "utf8"));
    assert.equal(old.issues.length, 1);
    assert.equal((await read()).issues.length, 0);
    await page.locator("#box-source").selectOption("visual");
    await page.locator("#layers-all").click();
    await page.locator("#tag").selectOption("a");
    if (!await page.locator("details:has(#nodes)").evaluate(element => element.hasAttribute("open")))
      await page.locator("details:has(#nodes) > summary").click();
    await page.locator("#nodes button").first().click();
    assert.equal(Number(await page.locator("#box-height").inputValue()), 100,
      "Visual review must use the image geometry, not the inline link line box");
    await page.locator("#issue-type").fill("框测试");
    await page.locator("#reason").fill("验证修正后的图片框坐标保存");
    await save(page);
    assert.equal((await read()).issues[0].bbox[3], 100,
      "Server must preserve validated visual geometry for new reviews");
    assert.deepEqual(errors, []);
    console.log("PASS: existing issue, scaled/scrolled bbox, persistence, edit/delete, isolation, conflicts, bounds and stale-capture archive");
  } finally {
    await browser?.close();
    await server?.close();
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

await test();
