import fs from "node:fs/promises";
import type http from "node:http";
import { serveDirectory } from "../infrastructure/static-server.js";
import { createReviewStore, ReviewError } from "./store.js";
import { publishReview } from "./page.js";

/** Limit annotation requests to avoid unbounded in-memory JSON payloads. */
async function readBody(request: http.IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const bytes = Buffer.from(chunk);
    size += bytes.length;
    if (size > 1024 * 1024) throw new ReviewError(413, "问题记录超过 1 MiB");
    chunks.push(bytes);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch { throw new ReviewError(400, "JSON 格式错误"); }
}

/** Serve the latest inspector and validated local human-feedback endpoints. */
export async function serveReview(directory: string, port = 4312) {
  const root = await fs.realpath(directory);
  await publishReview(root);
  const store = createReviewStore(root);
  return serveDirectory(root, port, async (request, response) => {
    const url = new URL(request.url ?? "/", "http://localhost");
    if (!url.pathname.startsWith("/api/")) return false;
    const json = (status: number, value: unknown) => {
      response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
      response.end(JSON.stringify(value));
    };
    try {
      const host = new URL(`http://${request.headers.host}`).hostname;
      if (!["127.0.0.1", "localhost", "[::1]"].includes(host))
        throw new ReviewError(403, "核验接口仅允许本机地址");
      const origin = request.headers.origin;
      if (origin && origin !== `http://${request.headers.host}`)
        throw new ReviewError(403, "不允许跨站修改核验记录");
      if (url.pathname === "/api/review-summary" && request.method === "GET") {
        json(200, await store.summary()); return true;
      }
      if (url.pathname !== "/api/review") throw new ReviewError(404, "未知接口");
      const pageId = url.searchParams.get("page") ?? "", viewportId = url.searchParams.get("viewport") ?? "";
      if (request.method === "GET") json(200, await store.get(pageId, viewportId));
      else if (request.method === "PUT") {
        if (!request.headers["content-type"]?.startsWith("application/json"))
          throw new ReviewError(415, "需要 application/json");
        json(200, await store.save(pageId, viewportId, await readBody(request)));
      } else throw new ReviewError(405, "仅支持 GET / PUT");
    } catch (error) {
      json(error instanceof ReviewError ? error.status : 500,
        { error: error instanceof Error ? error.message : String(error) });
    }
    return true;
  });
}
