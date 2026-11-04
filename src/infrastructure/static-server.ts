import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";

const contentTypes: Record<string, string> = {
  ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml",
  ".woff": "font/woff", ".woff2": "font/woff2", ".ico": "image/x-icon",
};

/** Optional routes run before static fallback; true means the response was handled. */
export type RequestHandler = (request: http.IncomingMessage, response: http.ServerResponse) => Promise<boolean>;

/** Serve an explicitly chosen static root on loopback; reject escaping symlinks/paths. */
export async function serveDirectory(root: string, port = 0, handler?: RequestHandler) {
  const absolute = await fs.realpath(root);
  const server = http.createServer((request, response) => {
    void (async () => {
      if (handler && await handler(request, response)) return;
      if (!["GET", "HEAD"].includes(request.method ?? "")) {
        response.writeHead(405).end(); return;
      }
      const name = decodeURIComponent(new URL(request.url ?? "/", "http://localhost").pathname);
      let file = path.resolve(absolute, `.${name}`);
      if (!file.startsWith(`${absolute}${path.sep}`) && file !== absolute) {
        response.writeHead(403).end(); return;
      }
      if ((await fs.stat(file)).isDirectory()) file = path.join(file, "index.html");
      file = await fs.realpath(file);
      if (!file.startsWith(`${absolute}${path.sep}`)) { response.writeHead(403).end(); return; }
      const content = await fs.readFile(file);
      response.writeHead(200, { "Content-Type": contentTypes[path.extname(file)] ?? "application/octet-stream" });
      response.end(request.method === "HEAD" ? undefined : content);
    })().catch(() => { if (!response.headersSent) response.writeHead(404); response.end(); });
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing server port");
  return {
    url: `http://127.0.0.1:${address.port}/`,
    close: async () => {
      server.closeAllConnections();
      await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    },
  };
}
