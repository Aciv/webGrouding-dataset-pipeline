/** Copied from view-collector/src/capture/online-preparation.ts.
 * Retains bounded URL preparation and screenshot retry; only scrollbar injection is removed.
 */
import type { Page } from "playwright";
import { waitForImageDecode } from "./image-readiness.js";

/** Online-only bounded preparation. Missing resources remain evidence, not a fatal gate. */
export async function prepareUrlCapture(page: Page, maxHeight: number, settleMs: number) {
    const deadline = Date.now() + 10000;
    const warnings: string[] = [];
    let preparedHeight = 0;
    let stable = false;
    // One initial sweep plus one follow-up for newly revealed content.
    for (let pass = 0; pass < 2 && Date.now() < deadline; pass++) {
        const geometry = await page.evaluate(() => ({ height: document.documentElement.scrollHeight, viewport: innerHeight }));
        const limit = Math.min(geometry.height, maxHeight);
        const step = Math.max(200, Math.floor(geometry.viewport * .8));
        for (let y = 0; y < limit && Date.now() < deadline; y += step) {
            const position = Math.min(y, Math.max(0, limit - geometry.viewport));
            await page.evaluate(value => scrollTo(0, value), position);
            preparedHeight = Math.max(preparedHeight, Math.min(limit, position + geometry.viewport));
            await page.waitForTimeout(Math.min(60, Math.max(0, deadline - Date.now())));
        }
        // ONE centralized settle barrier per lazy-scroll sweep.
        // Keep the 60ms scroll steps and follow-up sweep; outer capture/resize
        // callers must not stack their own settle timers on this preparation.
        // A second barrier is needed ONLY if the first sweep really grew the
        // document and required a follow-up (e.g. delayed infinite-scroll items).
        await page.waitForTimeout(Math.min(Math.max(100, settleMs), Math.max(0, deadline - Date.now())));
        const height = await page.evaluate(() => document.documentElement.scrollHeight);
        if (Math.min(height, maxHeight) <= preparedHeight) { stable = true; break; }
    }
    if (!stable) warnings.push(Date.now() >= deadline ? "preparation_budget_exhausted" : "height_unstable");
    await page.evaluate(() => scrollTo(0, 0));
    const images = await waitForImageDecode(page, preparedHeight, 3000);
    if (images.some(image => image.status !== "decoded")) warnings.push("images_incomplete");
    // Decoding is already a bounded readiness barrier. Only flush layout/paint
    // afterwards, with a timeout for background tabs where rAF may be throttled.
    await page.evaluate(() => new Promise<void>(resolve => {
        const timer = setTimeout(resolve, 100);
        requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(timer); resolve(); }));
    }));
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    if (Math.min(height, maxHeight) > preparedHeight && !warnings.includes("height_unstable"))
        warnings.push("height_changed_after_decode");
    return { method: "bounded_url_capture_v1", warnings, images,
        capturedHeight: Math.min(height, preparedHeight, maxHeight), scrollBudgetMs: 10000, imageBudgetMs: 3000 };
}

/** Retry only the screenshot operation, not scrolling or publishing a revision. */
export async function urlScreenshot(page: Page, options: Parameters<Page["screenshot"]>[0]): Promise<Buffer> {
    for (let attempt = 0; ; attempt++) {
        try { return await page.screenshot({ ...options, timeout: 15000 }); }
        catch (error) {
            if (attempt >= 1 || page.isClosed() || !(error instanceof Error) || !/timeout/i.test(error.message)) throw error;
        }
    }
}
