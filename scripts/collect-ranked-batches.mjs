#!/usr/bin/env node
/** Run prepared shards sequentially; each collector owns its configured website worker pool.
 * No shell interpolation and no second concurrency multiplier. Collection artifacts keep
 * their existing format under output/shards/NNN; this file only adds an aggregate index.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/** Validate the small orchestration interface before creating output or child processes. */
function options(args) {
  const values = { input: "examples/ranked-2000", output: "runs/initial-ranked-2000", resume: false, retry: false };
  for (let i = 0; i < args.length; i++) {
    const flag = args[i];
    if (flag === "--resume") values.resume = true;
    else if (flag === "--retry-incomplete") { values.retry = true; values.resume = true; }
    else if (["--input", "--output"].includes(flag)) {
      const value = args[++i];
      if (!value || value.startsWith("--")) throw new Error(`Missing value: ${flag}`);
      values[flag.slice(2)] = value;
    } else throw new Error(`Unknown option: ${flag}`);
  }
  return values;
}

/** Read a possibly absent checkpoint; malformed JSON and permission errors stay explicit. */
async function readOptional(file) {
  try { return JSON.parse(await fs.readFile(file, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }
}

/** Replace aggregate metadata atomically so interruption leaves the previous index readable. */
async function save(file, data) {
  const temporary = `${file}.tmp`;
  await fs.writeFile(temporary, JSON.stringify(data, null, 2) + "\n");
  await fs.rename(temporary, file);
}

/** Count complete responsive bundles and aggregate failures without declaring training GT valid. */
async function summarize(directory) {
  const batch = await readOptional(path.join(directory, "batch-index.json"));
  const discovery = await readOptional(path.join(directory, "discovery-index.json"));
  const sites = discovery?.sites ?? [];
  const pages = (batch?.pages ?? []).filter(page => page.status === "captured");
  const counts = { sites: sites.length, completed: 0, partial: 0, failed: 0, pages: pages.length,
    images: pages.reduce((sum, page) => sum + page.observations.filter(o => o.status === "captured").length, 0) };
  const errors = {};
  for (const site of sites) {
    if (site.status in counts) counts[site.status]++;
    if (site.error) errors[site.error] = (errors[site.error] ?? 0) + 1;
  }
  return { counts, errors };
}

/** Forward termination to the current collector and await its cleanup before exiting. */
async function main() {
  const flags = options(process.argv.slice(2));
  const input = path.resolve(flags.input), output = path.resolve(flags.output);
  const preparation = await readOptional(path.join(input, "preparation-report.json"));
  if (!preparation?.shards?.length) throw new Error("Input requires preparation-report.json with shards");
  await fs.access(path.join(project, "dist/cli.js"));
  await fs.mkdir(output, { recursive: true });
  const file = path.join(output, "collection-index.json");
  const previous = await readOptional(file);
  if (previous && !flags.resume) throw new Error("Output exists; use --resume or --retry-incomplete");
  // Pin actual effective configs, not just filenames, to reject accidental resume with other sources/settings.
  const configs = await Promise.all(preparation.shards.map(shard => readOptional(path.join(input, shard.config))));
  if (configs.some(value => !value)) throw new Error("Missing shard config");
  if (previous && JSON.stringify(previous.configs) !== JSON.stringify(configs))
    throw new Error("Resume requires the original effective shard configs");
  const index = previous ?? { schema: "ranked_collection_shards_v1", startedAt: new Date().toISOString(),
    annotationStatus: "rule_generated_unverified", input, configs,
    shards: preparation.shards.map((shard, i) => ({ config: shard.config,
      directory: `shards/${String(i + 1).padStart(3, "0")}`, status: "pending" })) };
  let child, interrupted = false;
  const stop = () => { interrupted = true; child?.kill("SIGINT"); };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  try {
    index.status = "running";
    await save(file, index);
    for (let i = 0; i < index.shards.length; i++) {
      if (interrupted) break;
      const shard = index.shards[i];
      const retry = flags.retry && ((shard.summary?.counts.partial ?? 0) + (shard.summary?.counts.failed ?? 0) > 0);
      if (shard.status === "completed" && !retry) continue;
      const directory = path.join(output, shard.directory);
      const hasBatch = await readOptional(path.join(directory, "batch-index.json"));
      shard.status = "running";
      shard.startedAt = new Date().toISOString();
      await save(file, index);
      if (interrupted) { shard.status = "interrupted"; break; }
      console.log(JSON.stringify({ type: "shard_started", shard: i + 1, total: index.shards.length,
        sources: configs[i].sources.length, websiteConcurrency: configs[i].concurrency }));
      const args = [path.join(project, "dist/cli.js"), "collect", "--input", path.join(input, shard.config),
        "--output", directory, ...(hasBatch ? ["--resume"] : [])];
      const result = await new Promise(resolve => {
        child = spawn(process.execPath, args, { cwd: project, stdio: "inherit" });
        let spawnError;
        child.once("error", error => { spawnError = error.message; });
        child.once("close", (code, signal) => resolve({ code, signal, spawnError }));
      });
      child = undefined;
      shard.status = interrupted ? "interrupted" : result.code === 0 ? "completed" : "failed";
      shard.process = result;
      shard.finishedAt = new Date().toISOString();
      shard.summary = await summarize(directory);
      index.totals = { completed: 0, partial: 0, failed: 0, pages: 0, images: 0 };
      for (const record of index.shards)
        for (const key of Object.keys(index.totals)) index.totals[key] += record.summary?.counts[key] ?? 0;
      index.updatedAt = new Date().toISOString();
      await save(file, index);
      console.log(JSON.stringify({ type: "shard_finished", shard: i + 1, status: shard.status,
        totals: index.totals, finishedShards: index.shards.filter(s => s.status === "completed").length,
        totalShards: index.shards.length }));
    }
    index.status = interrupted ? "interrupted" : index.shards.some(s => s.status === "failed") ? "has_failed_shards" : "completed";
    index.finishedAt = new Date().toISOString();
    await save(file, index);
    if (interrupted) process.exitCode = 130;
    else if (index.status === "has_failed_shards") process.exitCode = 1;
    console.log(JSON.stringify({ type: "collection_finished", status: index.status, index: file, totals: index.totals }));
  } finally {
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
