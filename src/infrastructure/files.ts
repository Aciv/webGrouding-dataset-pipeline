import fs from "node:fs/promises";
import path from "node:path";
import type { StageTimer } from "./timing.js";

/** Read required JSON without swallowing malformed or missing data. */
export async function readJson<T>(file: string): Promise<T> {
  return JSON.parse(await fs.readFile(file, "utf8")) as T;
}

/** Missing optional state is allowed; corruption and permission failures remain errors. */
export async function readOptionalJson<T>(file: string): Promise<T | undefined> {
  try { return await readJson<T>(file); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
}

/** Replace complete JSON atomically; serialized callers own checkpoint ordering. */
export async function writeJson(file: string, value: unknown, timer?: StageTimer): Promise<void> {
  const serialize = () => JSON.stringify(value, null, 2);
  const text = timer ? timer.measureSync("jsonSerializeMs", serialize) : serialize();
  const write = async () => {
    await fs.mkdir(path.dirname(file), { recursive: true });
    const temporary = `${file}.tmp`;
    await fs.writeFile(temporary, text);
    await fs.rename(temporary, file);
  };
  if (timer) await timer.measure("writeMs", write);
  else await write();
}
