import path from "node:path";
import { loadConfig } from "./config/load.js";
import { collect } from "./application/collect.js";
import { serveReview } from "./review/server.js";
import { auditBatch } from "./reporting/audit.js";
import { renderBboxes } from "./application/render-bboxes.js";
import { annotateLayers } from "./application/annotate-layers.js";

/** Minimal CLI: collect raw candidates, or serve the output inspector on loopback. */
async function main(): Promise<void> {
  const [command, ...args] = process.argv.slice(2);
  const allowed = command === "collect" ? ["--input", "--output", "--resume"]
    : ["audit", "boxes", "layers"].includes(command) ? ["--dir"] : ["--dir", "--port"];
  const flags = new Map<string, string>();
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (!allowed.includes(flag)) throw new Error(`Unknown argument: ${flag}`);
    if (flag === "--resume") { flags.set(flag, "true"); continue; }
    const value = args[++index];
    if (!value || value.startsWith("--")) throw new Error(`Missing value for ${flag}`);
    flags.set(flag, value);
  }
  if (command === "collect") {
    // --input: batch config; --output: new artifact directory; --resume: retry incomplete pages.
    const input = flags.get("--input"), output = flags.get("--output");
    if (!input || !output) throw new Error("collect --input batch.json --output runs/pilot [--resume]");
    const controller = new AbortController();
    const interrupt = () => controller.abort();
    process.once("SIGINT", interrupt);
    process.once("SIGTERM", interrupt);
    try {
      await collect(await loadConfig(path.resolve(input)), output, flags.has("--resume"), controller.signal);
      if (controller.signal.aborted) process.exitCode = 130;
    } finally {
      process.removeListener("SIGINT", interrupt);
      process.removeListener("SIGTERM", interrupt);
    }
  } else if (command === "layers") {
    // --dir: existing screenshot/DOM batch; generates layer/parent candidates without browsing.
    const summary = await annotateLayers(path.resolve(flags.get("--dir") ?? "runs/real-url-10"));
    console.log(JSON.stringify({ annotated: summary.observations.filter(item => item.status === "annotated").length,
      failed: summary.observations.filter(item => item.status === "failed").length }));
  } else if (command === "boxes") {
    // --dir: saved screenshot/DOM batch to convert into all-visible bbox JSON and PNG.
    const count = await renderBboxes(path.resolve(flags.get("--dir") ?? "runs/pilot-20"));
    console.log(JSON.stringify({ rendered: count }));
  } else if (command === "audit") {
    // --dir: artifact folder to verify; writes summary.json without re-rendering anything.
    console.log(JSON.stringify(await auditBatch(path.resolve(flags.get("--dir") ?? "runs/pilot-20")), null, 2));
  } else if (command === "review") {
    // --dir: completed/live output directory; --port: local inspector port, default 4312.
    const port = Number(flags.get("--port") ?? 4312);
    if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error("Invalid port");
    const server = await serveReview(path.resolve(flags.get("--dir") ?? "runs/pilot-20"), port);
    console.log(JSON.stringify({ url: server.url }));
    for (const signal of ["SIGINT", "SIGTERM"] as const)
      process.once(signal, () => { void server.close().then(() => process.exit(0)); });
  } else throw new Error("Commands: collect, review, audit, boxes, layers");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
