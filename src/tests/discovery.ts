/** Keep browser scenarios sequential so the suite does not multiply active Chromium workloads. */
await import("./discovery/state.js");
await import("./discovery/integration.js");
await import("./discovery/capture.js");
