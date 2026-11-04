import type { Page } from "playwright";
import type { PixelImage } from "../../domain/discovery/image-similarity.js";
import { MAX_REFERENCE_PIXELS } from "../../domain/discovery/image-similarity.js";
import { pngDimensions } from "../../infrastructure/png.js";

/** Decode an in-memory PNG using the same detached-canvas approach as bbox rendering.
 * Optional native-size top crop supports resume when initial.png was deliberately not saved.
 */
export async function decodePixels(page: Page, png: Buffer, crop?: { width: number; height: number }): Promise<PixelImage> {
  const { width, height } = pngDimensions(png);
  const selected = crop ?? { width, height };
  if (!selected.width || !selected.height || selected.width > width || selected.height > height ||
      selected.width * selected.height > MAX_REFERENCE_PIXELS || width * height > 40_000_000 || png.length > 32_000_000)
    throw new Error("Comparison image exceeds bounds");
  const encoded = await page.evaluate(async ({ base64, size }) => {
    const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }), 0, 0, size.width, size.height);
    try {
      const canvas = document.createElement("canvas");
      canvas.width = size.width; canvas.height = size.height;
      const context = canvas.getContext("2d")!;
      context.drawImage(bitmap, 0, 0);
      const data = context.getImageData(0, 0, size.width, size.height).data;
      let binary = "";
      for (let offset = 0; offset < data.length; offset += 32768)
        binary += String.fromCharCode(...data.subarray(offset, offset + 32768));
      return btoa(binary);
    } finally { bitmap.close(); }
  }, { base64: png.toString("base64"), size: selected });
  return { ...selected, data: Buffer.from(encoded, "base64") };
}
