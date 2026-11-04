/** CSS pixel rectangle; extraction uses viewport coordinates unless stated otherwise. */
export interface Box { x: number; y: number; width: number; height: number }

/** Explicit browser layout viewport, independent of screenshot pixel density. */
export interface Viewport { id: string; width: number; height: number }

/** Static directory is served locally; URL sources use their existing running server. */
export interface Source {
  id: string;
  kind: "static" | "url";
  root?: string;
  baseUrl?: string;
  repository?: string;
  license?: string;
  category?: string;
  /** Optional source display name retained from the imported URL candidate list. */
  name?: string;
  /** Optional same-origin pathname boundary for homepage link discovery. */
  pathPrefix?: string;
}

/** Optional diagnostic images; initial mode always requires initial.png as its canonical image. */
export interface ArtifactConfig { initial: boolean; overlayPng: boolean; overlaySvg: boolean }

/** Homepage-only URL discovery; child pages are captured but never recursively explored. */
export interface DiscoveryConfig {
  /** false: capture only the entry URL, without inventory, menu actions or image deduplication. */
  enabled?: boolean;
  minPages: number;
  maxPages: number;
  maxVisits: number;
  maxCandidates: number;
  menuInteractions: number;
  similarityThreshold: number;
  viewport: string;
}

/** One independent page job; path resolves against its source, never follows navigation. */
export interface PageInput { id: string; source: string; path: string }

/** Batch-wide settings; source directories are resolved relative to the JSON file. */
export interface Config {
  /** Only canonical image + hierarchy per viewport; checks/provenance remain in batch metadata. */
  exportOnly?: boolean;
  viewports: Viewport[];
  concurrency: number;
  navigationTimeoutMs: number;
  settleMs: number;
  maxNodes: number;
  /** Maximum captured document height in CSS pixels; viewport width stays unchanged. */
  maxHeight: number;
  /** initial captures only the top viewport without full-page scrolling; omitted keeps full mode. */
  captureMode?: "initial" | "full";
  artifacts?: ArtifactConfig;
  discovery?: DiscoveryConfig;
  sources: Source[];
  pages: PageInput[];
}

/** Unfiltered node facts. These are not COCO labels or semantic equivalence claims. */
export interface RawNode {
  /** WeakMap identity scoped to this document lifecycle; assigned to hidden nodes too. */
  nodeId: string;
  /** Native DOM parent; includes non-rendered wrappers. null means root or outside scope. */
  parentId: string | null;
  tag: string;
  selector: string;
  attributes: Record<string, string>;
  /** Direct text only, excluding descendant text; capped with an explicit flag. */
  ownText: string;
  textTruncated: boolean;
  /** Border-box fragments and their axis-aligned union, in viewport CSS pixels. */
  bbox: Box;
  fragments: Box[];
  documentBbox: Box;
  /** Measured inner client/scrollport bounds; absent on older observations. */
  clientBbox?: Box;
  /** Border-box intersection with screenshot, not an occlusion-corrected pixel mask. */
  captureBbox: Box | null;
  /** Direct text-node line rectangles, separate from the complete control bbox. */
  textRects: Box[];
  textRectsTruncated: boolean;
  style: Record<string, string>;
  /** Hint inherited from the View extractor, not proof of behavior or visual class. */
  interactiveHint: boolean;
  visibility: {
    cssVisible: boolean;
    intersectsViewport: boolean;
    /** Ancestor overflow may clip the box; exact visible mask is not computed. */
    clippingAncestors: string[];
    occlusion: "not_measured";
  };
  pseudo: { before: string; after: string };
}

/** Geometry checkpoint; no training categories are assigned at extraction time. */
export interface RawSnapshot {
  schema: "raw_dom_geometry_v1";
  documentId: string;
  url: string;
  title: string;
  capturedAt: string;
  viewport: { width: number; height: number; dpr: number };
  scroll: { x: number; y: number };
  document: { width: number; height: number };
  /** Canonical image range; truncated indicates document content outside the saved image. */
  capture?: {
    image: "full.png" | "initial.png";
    width: number;
    height: number;
    maxHeight: number;
    truncated: boolean;
  };
  coordinateSpace: "viewport_css_pixels";
  scope: "main_frame_light_dom";
  totalNodes: number;
  truncated: boolean;
  nodes: RawNode[];
}

/** Per-resolution result; complete means files exist, not that labels are validated. */
export interface ObservationResult {
  viewport: string;
  status: "captured" | "failed";
  directory: string;
  elapsedMs: number;
  nodes?: number;
  visibleCandidates?: number;
  stable?: boolean;
  warnings?: string[];
  error?: string;
  /** Actual source URL/document and layout viewport, retained even without raw DOM files. */
  captureIdentity?: { url: string; documentId: string; viewport: { width: number; height: number } };
  /** Stage wall times include browser/I/O waits; sums across concurrent pages are not wall-clock time. */
  timings?: Record<string, number>;
  captureCounts?: { attempts: number; domExtractions: number; initialScreenshots: number; fullScreenshots: number };
  /** Compact mode preserves preparation, asset and popup evidence in the index, rather than separate files. */
  quality?: Record<string, unknown>;
}

/** Persisted page job state supports inspection and retries without losing successes. */
export interface PageResult {
  id: string;
  source: string;
  path: string;
  status: "pending" | "running" | "captured" | "partial" | "failed";
  observations: ObservationResult[];
  elapsedMs?: number;
  error?: string;
  /** Requested/final page identities and provenance, present for URL discovery outputs. */
  url?: string;
  urlIdentity?: string;
  title?: string;
  discoveredFrom?: string | null;
  discoveredAt?: string;
}
