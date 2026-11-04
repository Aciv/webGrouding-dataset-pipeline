/** Read PNG dimensions after validating the signature/header; this is not full pixel decoding. */
export function pngDimensions(buffer: Buffer): { width: number; height: number } {
  if (buffer.length < 24 || !buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])))
    throw new Error("Invalid PNG header");
  const width = buffer.readUInt32BE(16), height = buffer.readUInt32BE(20);
  if (!width || !height) throw new Error("Empty PNG dimensions");
  return { width, height };
}
