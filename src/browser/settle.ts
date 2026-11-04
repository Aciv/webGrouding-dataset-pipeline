import type { Page } from "playwright";

/** Port of src/dataset-collection/browser/page-runtime.ts::settle.
 * Preserve resource/font bounds and the two-stable-read geometry check.
 * Only images and boxes intersecting the capture range participate: changes below
 * maxHeight must not prevent a deliberately capped capture from settling.
 */
export async function settleCapture(page: Page, settleMs: number, maxHeight: number): Promise<void> {
    await page.waitForLoadState("domcontentloaded").catch(() => {});
    await page.waitForLoadState("networkidle", { timeout: 750 }).catch(() => {});
    await page.evaluate(async height => {
        const ready = Promise.all([
            document.fonts?.ready?.catch(() => {}),
            ...[...document.images].filter(image => {
                const rect = image.getBoundingClientRect();
                return !image.complete && rect.width > 0 && rect.top + scrollY < height && rect.bottom + scrollY > 0;
            }).map(image => new Promise(resolve => {
                image.addEventListener("load", resolve, { once: true });
                image.addEventListener("error", resolve, { once: true });
            })),
        ]);
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
            await Promise.race([ready, new Promise(resolve => { timer = setTimeout(resolve, 6000); })]);
        } finally { if (timer !== undefined) clearTimeout(timer); }
    }, maxHeight);
    await page.waitForTimeout(settleMs);
    const signature = () => page.evaluate(height => {
        const animated = new WeakMap<Element, boolean>();
        const endlessMotion = (element: Element): boolean => {
            const cached = animated.get(element);
            if (cached !== undefined) return cached;
            const style = getComputedStyle(element);
            const names = style.animationName.split(",").map(value => value.trim());
            const iterations = style.animationIterationCount.split(",").map(value => value.trim());
            const durations = style.animationDuration.split(",").map(value => parseFloat(value));
            const states = style.animationPlayState.split(",").map(value => value.trim());
            const own = names.some((name, index) => name !== "none" &&
                iterations[index % iterations.length] === "infinite" &&
                durations[index % durations.length] > 0 && states[index % states.length] === "running");
            const result = own || Boolean(element.parentElement && endlessMotion(element.parentElement));
            animated.set(element, result);
            return result;
        };
        return JSON.stringify({ width: document.documentElement.scrollWidth,
            height: Math.min(document.documentElement.scrollHeight, height),
            boxes: [...document.querySelectorAll("a,button,input,select,textarea,main,nav,img")]
                .slice(0, 300).filter(element => !endlessMotion(element)).flatMap(element => {
                    const box = element.getBoundingClientRect();
                    if (box.top + scrollY >= height || box.bottom + scrollY <= 0) return [];
                    return [[Math.round(box.x), Math.round(box.y), Math.round(box.width),
                        Math.round(box.height), getComputedStyle(element).display]];
                }),
        });
    }, maxHeight);
    let previous = await signature(), stable = 0;
    for (let attempt = 0; attempt < 12; attempt++) {
        await page.waitForTimeout(100);
        const current = await signature();
        stable = current === previous ? stable + 1 : 0;
        if (stable >= 2) return;
        previous = current;
    }
    throw new Error("Layout did not stabilize within the capture window");
}
