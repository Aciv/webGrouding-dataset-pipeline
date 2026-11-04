/** Wall time by non-overlapping local stages; concurrent pages may overlap in real time. */
export interface StageTimings { [stage: string]: number }

/** Accumulate stage durations without retaining DOM, buffers or per-event histories. */
export class StageTimer {
  readonly ms: StageTimings = {};

  /** Time awaited browser/I/O work, including its failures. */
  async measure<T>(stage: string, action: () => Promise<T>): Promise<T> {
    const start = performance.now();
    try { return await action(); }
    finally { this.ms[stage] = (this.ms[stage] ?? 0) + performance.now() - start; }
  }

  /** Time synchronous Node computation without pretending Promise concurrency is parallelism. */
  measureSync<T>(stage: string, action: () => T): T {
    const start = performance.now();
    try { return action(); }
    finally { this.ms[stage] = (this.ms[stage] ?? 0) + performance.now() - start; }
  }

  /** Return compact rounded values; callers define the total measurement boundary. */
  snapshot(): StageTimings {
    return Object.fromEntries(Object.entries(this.ms).map(([key, value]) => [key, Math.round(value * 10) / 10]));
  }
}
