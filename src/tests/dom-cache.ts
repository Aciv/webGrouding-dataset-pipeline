import assert from "node:assert/strict";
import { chromium } from "playwright";
import { extractDom } from "../browser/dom.js";
import { extractDomReference } from "./fixtures/dom-extraction-v1.js";
import type { RawSnapshot } from "../domain/types.js";

/** Only the wall-clock timestamp may differ between observations of an unchanged fixture. */
function withoutTime(snapshot: RawSnapshot) {
  const { capturedAt: _time, ...facts } = snapshot;
  return facts;
}

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 900, height: 600 } });
  await page.setContent(`<!doctype html><style>
    body{margin:0}#outer{overflow:hidden;width:700px;height:500px}
    #inner{overflow:auto;width:400px;height:350px}button::before{content:'+'}
    .hidden{display:none}.row{height:20px}.row span{display:inline-block}
    </style><main id="outer"><section id="inner"><button id="unique:button">Go</button>
    <div id="duplicate">One</div><div id="duplicate">Two</div>
    <svg width="100" height="50"><g><text x="0" y="20">SVG</text></g></svg>
    <div class="hidden"><span>Hidden child</span></div>
    ${Array.from({ length: 400 }, (_, i) => `<div class="row"><span>Item ${i}</span><a href="/${i}">Open</a></div>`).join("")}
    ${"<article>".repeat(90)}<em>Deep text</em>${"</article>".repeat(90)}
    </section></main>`);

  /** Compare every raw field, not only selected boxes or downstream label counts. */
  const compare = async (limit = 20000) => {
    const old = await extractDomReference(page, limit, 2000);
    const current = await extractDom(page, limit, 2000);
    assert.deepEqual(withoutTime(current), withoutTime(old));
    return current;
  };
  await compare();
  await compare(25); // All-document ID uniqueness and identity must survive truncation.
  await page.setViewportSize({ width: 390, height: 844 });
  await compare();
  await page.evaluate(() => {
    const inner = document.querySelector("#inner")!;
    inner.insertAdjacentHTML("afterbegin", '<button id="unique:button">New duplicate</button>');
    (inner as HTMLElement).style.overflow = "visible";
    document.querySelector("article")!.remove();
  });
  await compare(); // No cache may survive a mutation, new sibling or changed overflow.

  await page.setContent('<div id="UPPER">A</div><div id="upper">B</div><p id="other">C</p>');
  assert.equal(await page.evaluate(() => document.compatMode), "BackCompat");
  await compare(); // CSS ID selectors are ASCII case-insensitive in quirks mode.
  await page.evaluate(() => { document.querySelector("p")!.id = "nul\0id"; });
  await compare();

  // HTML named properties can shadow form.children; the new sibling pass must not read it.
  await page.setContent('<!doctype html><form><input name="children"><button>Submit</button></form>');
  const named = await extractDom(page, 100);
  assert(named.nodes.some(node => node.tag === "button"));
  console.log("PASS: cached DOM output equals the original extractor across dense/deep DOM, clipping, truncation, resize, mutations, quirks IDs and named forms");
} finally {
  await browser.close();
}
