import type { Page } from "playwright";

/** Copied from src/dataset-collection/browser/image-readiness.ts: one shared decode deadline. */
export interface ImageDecodeRecord {
    url: string;
    status: "decoded" | "decode_error" | "pending_lazy" | "timed_out";
}

/** Restrict readiness to the captured document range. Unlike the old global helper,
 * lazy images inside this already-scrolled range must decode too; images below the cap
 * are excluded from the decode barrier. This does not suppress native browser prefetch.
 */
export async function waitForImageDecode(page: Page, maxHeight: number,
    maxWaitMs = 3000): Promise<ImageDecodeRecord[]> {
    return page.evaluate(async ({ height, timeoutMs }) => {
        const records: ImageDecodeRecord[] = [];
        const tasks: Promise<void>[] = [];
        const deadline = Date.now() + timeoutMs;
        let stopped = false;
        for (const image of document.images) {
            const rect = image.getBoundingClientRect();
            // Preserve the old width-only admission: unloaded images can have zero
            // intrinsic height. Excluding them would recreate the premature-capture bug.
            if (rect.width <= 0 || rect.top + scrollY >= height || rect.bottom + scrollY < 0 ||
                rect.left + scrollX >= innerWidth || rect.right + scrollX <= 0) continue;
            const record: ImageDecodeRecord = { url: image.currentSrc || image.src,
                status: !image.currentSrc && !image.getAttribute("src") ? "pending_lazy" : "timed_out" };
            records.push(record);
            tasks.push((async () => {
                // Scroll handlers may assign src asynchronously. Do not immediately return
                // just because an empty-src image currently reports complete=true.
                while (!image.currentSrc && !image.getAttribute("src") && !stopped && Date.now() < deadline)
                    await new Promise(resolve => setTimeout(resolve, Math.min(50, Math.max(0, deadline - Date.now()))));
                if (stopped || !image.currentSrc && !image.getAttribute("src")) return;
                record.url = image.currentSrc || image.src;
                await image.decode().then(() => { record.status = "decoded"; }, () => { record.status = "decode_error"; });
            })());
        }
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
            await Promise.race([Promise.all(tasks), new Promise<void>(resolve => {
                timer = setTimeout(resolve, timeoutMs);
            })]);
            return records.map(record => ({ ...record }));
        } finally { stopped = true; if (timer !== undefined) clearTimeout(timer); }
    }, { height: maxHeight, timeoutMs: Math.max(0, maxWaitMs) });
}
