import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright";
import { captureViewport } from "../browser/capture.js";
import { removeCapturePopups } from "../browser/popup-cleanup.js";
import type { Config } from "../domain/types.js";

const root = await fs.mkdtemp(path.join(os.tmpdir(), "ui-popups-"));
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.route("http://localhost/**", route => route.fulfill({ body: "<!doctype html>" }));
  await page.goto("http://localhost/");
  await page.setContent(`<!doctype html><style>
    body{margin:0;overflow:hidden}nav{position:fixed;top:0;background:green}
    .modal-backdrop{position:fixed;inset:0;background:black}
    #cookie-banner{position:fixed;bottom:0;background:red}
    #chat-launcher{position:fixed;right:0;bottom:0}
    </style><nav id="nav">Navigation</nav><main><h1>Content</h1></main>
    <div class="modal-backdrop"></div>
    <div role="dialog" id="subscribe"><button>Subscribe</button></div>
    <div id="cookie-banner"><button>Accept cookies</button></div>
    <button id="chat-launcher">Chat</button>
    <div id="actionbar" class="actnbr-pub-kelly" style="position:fixed;top:60px">Subscribe</div>
    <div id="marketingbar" class="marketing-bar" style="position:fixed;top:0">Start free</div>
    <div class="wordads-ad-sticky-wrapper" style="position:fixed;top:100px">Advertisement</div>
    <div id="native-popover" popover>Native popover</div>
    <dialog id="native"><p>Native modal</p></dialog>`);
  await page.evaluate(() => (document.querySelector("dialog") as HTMLDialogElement).showModal());
  const removed = await removeCapturePopups(page);
  assert(removed.some(item => item.id === "native"));
  assert.equal(await page.locator("#native").count(), 0);
  assert.equal(await page.locator("#nav").isVisible(), true);
  assert.equal(await page.locator("#chat-launcher").isVisible(), true);
  assert.equal(await page.locator(".modal-backdrop").isVisible(), false);
  assert.equal(await page.locator("#cookie-banner").isVisible(), false);
  for (const selector of ["#actionbar", "#marketingbar", ".wordads-ad-sticky-wrapper"])
    assert.equal(await page.locator(selector).isVisible(), true, selector);
  assert.deepEqual(await removeCapturePopups(page), []);
  await page.locator("#native-popover").evaluate(node => (node as HTMLElement).showPopover());
  await removeCapturePopups(page);
  assert.equal(await page.locator("#native-popover").count(), 0);
  // A popup that appears during preparation must be hidden before the image and boxes.
  await page.evaluate(() => setTimeout(() => {
    const modal = document.createElement("div");
    modal.id = "late-popup";
    modal.setAttribute("role", "dialog");
    modal.textContent = "Late popup";
    document.body.append(modal);
  }, 10));
  const config: Config = { viewports: [], sources: [], pages: [], concurrency: 1,
    navigationTimeoutMs: 5000, settleMs: 30, maxNodes: 500, maxHeight: 1000,
    captureMode: "initial", artifacts: { initial: true, overlayPng: false, overlaySvg: false } };
  await captureViewport(page, { id: "desktop", width: 640, height: 480 }, config, root, "capture");
  const boxes = JSON.parse(await fs.readFile(path.join(root, "capture/bboxes.json"), "utf8"));
  assert(!boxes.elements.some((item: { selector: string }) => /subscribe|cookie-banner|late-popup|native/.test(item.selector)));
  assert(boxes.elements.some((item: { selector: string }) => item.selector === "#nav"));
  const quality = JSON.parse(await fs.readFile(path.join(root, "capture/quality.json"), "utf8"));
  const raw = await fs.readFile(path.join(root, "capture/nodes.json"), "utf8");
  const html = await fs.readFile(path.join(root, "capture/dom.html"), "utf8");
  assert(!raw.includes('"late-popup"'));
  assert(!html.includes('id="late-popup"'));
  assert(quality.removedPopups.some((item: { id: string }) => item.id === "late-popup"));
  assert.equal(quality.stable, true);
  await page.setContent(`<style>body{margin:0}p{margin:0;height:30px}
    #float{position:fixed;bottom:0;left:0;width:300px;height:60px;background:white}
    #empty-footer{position:fixed;bottom:0;right:0;width:100px;height:60px;background:white}
    nav{position:fixed;top:0}</style>
    <main style="width:300px">${'<p>Underlying article text covering the width</p>'.repeat(50)}</main>
    <div id="float">Floating actions</div><div id="empty-footer">Reserved space</div><nav>Navigation</nav>`);
  const covering = await removeCapturePopups(page);
  assert(covering.some(item => item.id === "float" && item.reason === "bottom_float_covering_content"));
  assert.equal(await page.locator("#float").count(), 0);
  assert.equal(await page.locator("#empty-footer").count(), 1);
  assert.equal(await page.locator("nav").count(), 1);
  console.log("PASS: popup/backdrop removal, native modal close, delayed popup capture, normal fixed UI retained");
} finally {
  await browser.close();
  await fs.rm(root, { recursive: true, force: true });
}
