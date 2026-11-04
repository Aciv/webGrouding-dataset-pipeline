import fs from "node:fs/promises";

// Keep browser UI source separate from TypeScript templates and publish it with dist.
await fs.cp(new URL("../src/review/public/", import.meta.url),
  new URL("../dist/review/public/", import.meta.url), { recursive: true });
// Share precisely the same geometry projection between exporters and the inspector.
await fs.copyFile(new URL("../dist/domain/visibility.js", import.meta.url),
  new URL("../dist/review/public/visibility.js", import.meta.url));
await fs.copyFile(new URL("../dist/domain/css-visibility.js", import.meta.url),
  new URL("../dist/review/public/css-visibility.js", import.meta.url));
