import type { Page } from "playwright";
import { waitForImageDecode } from "./image-readiness.js";

/** Prepare only the top viewport: bounded fonts/image waits, no lazy-scroll sweep or full-page measurement. */
export async function prepareInitialCapture(page: Page, height: number, settleMs: number) {
  await page.evaluate(() => scrollTo({ left: 0, top: 0, behavior: "instant" }));
  await page.waitForTimeout(settleMs);
  await page.evaluate(async () => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([document.fonts.ready.catch(() => {}), new Promise<void>(resolve => {
        timer = setTimeout(resolve, 3000);
      })]);
    } finally { if (timer !== undefined) clearTimeout(timer); }
  });
  const images = await waitForImageDecode(page, height, 3000);
  await page.evaluate(() => new Promise<void>(resolve => {
    const timer = setTimeout(resolve, 100);
    requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(timer); resolve(); }));
  }));
  return { method: "initial_viewport_v1", images,
    warnings: images.some(image => image.status !== "decoded") ? ["images_incomplete"] : [],
    capturedHeight: height, scrollBudgetMs: 0, imageBudgetMs: 3000 };
}
