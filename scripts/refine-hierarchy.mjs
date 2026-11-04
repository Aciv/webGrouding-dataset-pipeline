#!/usr/bin/env node
/** Refine old two-file exports offline; share the collector's geometry rules. */
import fs from "node:fs/promises";
import path from "node:path";
import { constants } from "node:fs";
import { refineSavedHierarchy } from "../dist/domain/hierarchy/refine-saved.js";

/** Parse a deliberately small interface; source and output must be disjoint. */
function options(args) {
  const flags = new Map();
  for (let index = 0; index < args.length; index += 2) {
    if (!["--input", "--output"].includes(args[index]) || !args[index + 1] || args[index + 1].startsWith("--"))
      throw new Error("Usage: node scripts/refine-hierarchy.mjs --input OLD --output NEW");
    flags.set(args[index], args[index + 1]);
  }
  if (!flags.has("--input") || !flags.has("--output")) throw new Error("--input and --output are required");
  return { input: path.resolve(flags.get("--input")), output: path.resolve(flags.get("--output")) };
}

/** Ignore in-flight/hidden trees; keep original batch wrappers and URL indices. */
async function scan(root, relative = "", result = { labels: [], metadata: [] }) {
  for (const entry of await fs.readdir(path.join(root, relative), { withFileTypes: true })) {
    if (entry.name.startsWith(".") || entry.isSymbolicLink()) continue;
    const name = path.join(relative, entry.name);
    if (entry.isDirectory()) await scan(root, name, result);
    else if (entry.name === "hierarchy.json" && (root.split(path.sep).includes("pages") || name.split(path.sep).includes("pages")))
      result.labels.push(name);
    else if (["collection-index.json", "batch-index.json", "url-history.jsonl"].includes(entry.name))
      result.metadata.push({ from: path.join(root, name), relative: name });
  }
  return result;
}

/** Reuse image bytes without doubling disk space on the same filesystem. */
async function linkOrCopy(source, destination) {
  try { await fs.link(source, destination); return "hardlink"; }
  catch (error) {
    if (!["EXDEV", "EPERM", "EOPNOTSUPP", "ENOTSUP"].includes(error.code)) throw error;
    await fs.copyFile(source, destination, constants.COPYFILE_FICLONE);
    return "copy";
  }
}

/** Publish each pair atomically and preserve original labels/images for rollback. */
async function main() {
  const { input, output } = options(process.argv.slice(2));
  const source = await fs.realpath(input);
  if (!(await fs.stat(source)).isDirectory()) throw new Error("Input must be a directory");
  if (output === source || output.startsWith(source + path.sep) || source.startsWith(output + path.sep))
    throw new Error("Input and output must not contain each other");
  try { if ((await fs.readdir(output)).length) throw new Error("Output must be empty"); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  const files = await scan(source);
  if (!files.labels.length) throw new Error("No pages/*/*/hierarchy.json found");
  const prefix = path.basename(source) === "pages" ? "pages" : "";
  if (prefix) for (const name of ["collection-index.json", "batch-index.json", "url-history.jsonl"]) {
    const from = path.join(path.dirname(source), name);
    try { await fs.access(from); files.metadata.push({ from, relative: name }); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  const report = { status: "completed", source, output, observations: [], before: 0, after: 0, failed: 0 };
  for (const relative of files.labels.sort()) {
    const temporary = path.join(output, ".refine-staging", String(report.observations.length));
    try {
      const hierarchy = JSON.parse(await fs.readFile(path.join(source, relative), "utf8"));
      // Old explicit-page indices sometimes omit the final URL; retain the real
      // saved DOM URL in the compact label instead of copying bulky DOM files.
      if (!hierarchy.capture.url) {
        try {
          const raw = JSON.parse(await fs.readFile(path.join(source, path.dirname(relative), "nodes.json"), "utf8"));
          if (raw.url) hierarchy.capture.url = raw.url;
        } catch (error) { if (error.code !== "ENOENT") throw error; }
      }
      const image = hierarchy.capture.image;
      if (!["full.png", "initial.png"].includes(image)) throw new Error(`Unsupported image: ${image}`);
      const refined = refineSavedHierarchy(hierarchy);
      await fs.mkdir(temporary, { recursive: true });
      const imageMode = await linkOrCopy(path.join(source, path.dirname(relative), image), path.join(temporary, image));
      await fs.writeFile(path.join(temporary, "hierarchy.json"), JSON.stringify(refined, null, 2) + "\n");
      const destination = path.join(output, prefix, path.dirname(relative));
      await fs.mkdir(path.dirname(destination), { recursive: true });
      await fs.rename(temporary, destination);
      report.before += hierarchy.nodes.length;
      report.after += refined.nodes.length;
      report.observations.push({ file: path.join(prefix, relative), status: "refined", before: hierarchy.nodes.length,
        after: refined.nodes.length, imageMode,
        priorityFallback: hierarchy.nodes.some(node => node.selection_priority === undefined) });
    } catch (error) {
      await fs.rm(temporary, { recursive: true, force: true });
      report.failed++;
      report.observations.push({ file: relative, status: "failed", error: error.message });
    }
  }
  for (const item of files.metadata) {
    const destination = path.join(output, item.relative);
    await fs.mkdir(path.dirname(destination), { recursive: true });
    await fs.copyFile(item.from, destination);
  }
  await fs.rm(path.join(output, ".refine-staging"), { recursive: true, force: true });
  report.status = report.failed ? "partial" : "completed";
  await fs.writeFile(path.join(output, "hierarchy-cleanup-report.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ status: report.status, observations: report.observations.length,
    failed: report.failed, before: report.before, after: report.after,
    report: path.join(output, "hierarchy-cleanup-report.json") }));
  if (report.failed) process.exitCode = 1;
}

main().catch(error => { console.error(error); process.exitCode = 1; });
