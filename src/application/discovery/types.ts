import type { Browser } from "playwright";
import type { Config, DiscoveryConfig, PageInput, PageResult, Source, Viewport } from "../../domain/types.js";
import type { SiteDiscovery } from "../../domain/discovery/types.js";

/** Persisted pages/sites plus transient ID claims shared by the bounded source workers. */
export interface DiscoveryState {
  root: string;
  config: Config;
  settings: DiscoveryConfig;
  reference: Viewport;
  startedAt: string;
  pages: PageResult[];
  sites: SiteDiscovery[];
  /** Reserve configured entry IDs before any asynchronous publication can allocate a child ID. */
  reservedIds: Set<string>;
}

/** Parsed configuration and mutable state are shared directly, never converted into another config. */
export interface SiteTask {
  state: DiscoveryState;
  site: SiteDiscovery;
  source: Source;
  seed: PageInput;
  checkpoint: () => Promise<void>;
  /** Runtime cancellation, never serialized into configuration or manifests. */
  signal?: AbortSignal;
}

/** Browser ownership belongs to the outer workflow; each site owns only its context/page. */
export interface SiteWorkerTask extends SiteTask { browser: Browser }
