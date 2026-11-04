import type { Page } from "playwright";
import type { Viewport } from "../domain/types.js";

/** Same-page responsive sampling, extracted from controller.batch and URL references.
 * No navigation/replay and no cross-size DOM matching: each size observes this page.
 * Hooks preserve human viewport controls and each workflow's per-size error policy.
 */
export async function captureResponsive(page: Page, sizes: readonly Viewport[],
    capture: (size: Viewport) => Promise<void>, options: {
        resize?: (size: Viewport) => Promise<void>;
        restore?: () => Promise<void>;
        restoreScroll?: boolean;
    } = {}): Promise<void> {
    const original = page.viewportSize();
    if (!original) throw new Error("Responsive capture needs a fixed viewport");
    const scroll = await page.evaluate(() => ({ x: scrollX, y: scrollY }));
    try {
        for (const size of sizes) {
            // The capture hook owns resize when it needs to record per-size failures.
            if (options.resize) await options.resize(size);
            await capture(size);
        }
    } finally {
        if (!page.isClosed()) {
            if (options.restore) await options.restore();
            else await page.setViewportSize(original);
            if (options.restoreScroll)
                await page.evaluate(position => scrollTo(position.x, position.y), scroll);
        }
    }
}
