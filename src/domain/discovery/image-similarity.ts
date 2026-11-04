/** Copied metric from view-collector/src/recognition/images.ts; decoder/lifecycle kept separate. */
export interface PixelImage { width: number; height: number; data: Uint8Array }
export const PIXEL_CHANNEL_TOLERANCE = 32;
/** The original View comparison budget bounds decoded reference memory per selected page. */
export const MAX_REFERENCE_PIXELS = 4_000_000;

/** Native-size pixel agreement: every RGB channel must be within tolerance, alpha over white. */
export function pixelSimilarity(a: PixelImage, b: PixelImage): number {
  if (a.width !== b.width || a.height !== b.height || !a.width || !a.height ||
      a.data.length !== a.width * a.height * 4 || b.data.length !== b.width * b.height * 4)
    throw new Error("Image dimensions differ");
  let matched = 0;
  for (let offset = 0; offset < a.data.length; offset += 4) {
    const alphaA = a.data[offset + 3] / 255, alphaB = b.data[offset + 3] / 255;
    let equal = true;
    for (let channel = 0; channel < 3; channel++) {
      const difference = Math.abs(a.data[offset + channel] * alphaA + 255 * (1 - alphaA) -
        b.data[offset + channel] * alphaB - 255 * (1 - alphaB));
      if (difference > PIXEL_CHANNEL_TOLERANCE) { equal = false; break; }
    }
    if (equal) matched++;
  }
  return matched / (a.width * a.height);
}
