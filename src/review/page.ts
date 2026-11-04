import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

/** Refresh viewer assets while preserving captures and human feedback. */
export async function publishReview(root: string): Promise<void> {
  const assets = fileURLToPath(new URL("./public/", import.meta.url));
  await fs.cp(assets, path.join(root, "review-assets"), { recursive: true });
  await fs.copyFile(path.join(assets, "index.html"), path.join(root, "index.html"));
}
