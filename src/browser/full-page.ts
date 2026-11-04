import type { Page } from "playwright";
import { waitForImageDecode, type ImageDecodeRecord } from "./image-readiness.js";
import { settleCapture } from "./settle.js";

/** Options mirror the old primeFullPage; a deadline means best-effort recognition,
 * never the normal screenshot path. maxHeight always bounds the visited document range.
 */
export interface FullPagePrimeOptions {
    deadline?: number;
    maxHeight: number;
    restoreScroll?: boolean;
}

/** Port of src/dataset-collection/navigation/state.ts::primeFullPage (not a new algorithm).
 * Keep its three-pass/two-equal-height check, 60ms steps and bottom settlement;
 * reaching the fixed cap on the final pass is a bounded-range completion exception.
 * Deliberate changes: clamp every scroll to the capture cap, decode only in-range images,
 * and reject an unfinished official capture instead of silently saving unloaded pixels.
 */
export async function primeFullPage(page: Page, settleMs: number,
    options: FullPagePrimeOptions): Promise<ImageDecodeRecord[]> {
    // Local migration change: retain native scrollbar layout, matching DOM extraction.
    const original = await page.evaluate(() => ({ x: scrollX, y: scrollY }));
    const bounded = options.deadline !== undefined;
    const alive = () => options.deadline === undefined || Date.now() < options.deadline;
    const wait = async (milliseconds: number) => {
        if (!alive()) return;
        await page.waitForTimeout(Math.min(milliseconds,
            options.deadline === undefined ? milliseconds : Math.max(0, options.deadline - Date.now())));
    };
    let previousHeight = 0;
    let stable = false;
    let preparedHeight = 0;
    let readiness: ImageDecodeRecord[] = [];
    try {
        for (let pass = 0; pass < 3 && alive(); pass++) {
            const { height, viewport } = await page.evaluate(() => ({
                height: document.documentElement.scrollHeight, viewport: innerHeight,
            }));
            const limit = Math.min(height, options.maxHeight);
            const step = Math.max(200, Math.floor(viewport * .8));
            for (let position = 0; position < limit && alive(); position += step) {
                await page.evaluate(y => window.scrollTo(0, y), Math.min(position, Math.max(0, limit - viewport)));
                await wait(60);
            }
            if (!alive()) break;
            await page.evaluate(maximum => window.scrollTo(0,
                Math.max(0, Math.min(document.documentElement.scrollHeight, maximum) - innerHeight)), limit);
            await wait(Math.max(100, settleMs));
            const currentHeight = await page.evaluate(() => document.documentElement.scrollHeight);
            preparedHeight = limit;
            if (Math.min(currentHeight, options.maxHeight) === limit &&
                (limit === previousHeight || pass === 2 && limit === options.maxHeight)) {
                stable = true;
                break;
            }
            previousHeight = limit;
        }
        if (!stable && !bounded) throw new Error("Page height kept growing within capture height limit");
        if (!bounded) {
            readiness = await waitForImageDecode(page, options.maxHeight);
            if (readiness.some(image => image.status === "timed_out" || image.status === "pending_lazy"))
                throw new Error("Full-page preparation incomplete: in-range lazy images did not load within 3000ms");
        }
    } finally {
        await page.evaluate(position => window.scrollTo(position.x, position.y),
            options.restoreScroll ? original : { x: 0, y: 0 }).catch(() => {});
    }
    if (bounded) await wait(settleMs);
    else {
        await settleCapture(page, settleMs, options.maxHeight);
        const height = await page.evaluate(() => document.documentElement.scrollHeight);
        if (Math.min(height, options.maxHeight) > preparedHeight)
            throw new Error("Page height grew after decoding; capture range was not fully prepared, retry capture");
    }
    return readiness;
}
