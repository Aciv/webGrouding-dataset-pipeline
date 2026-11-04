/** A declared homepage destination; this records provenance, not a verified click transition. */
export interface LinkCandidate {
  url: string;
  fromUrl: string | null;
  method: "entry" | "href" | "menu_href" | "menu_navigation";
  selector?: string;
  menuSelector?: string;
  text?: string;
}

/** Every visited URL has an outcome even when no dataset page is published. */
export interface DiscoveryVisit {
  candidate: LinkCandidate;
  startedAt: string;
  status: "running" | "capturing" | "accepted" | "partial" | "duplicate_url" | "duplicate_image" | "failed" | "interrupted";
  finalUrl?: string;
  pageId?: string;
  candidatePageId?: string;
  duplicateOf?: string;
  similarity?: number;
  closestPageId?: string;
  comparisonReference?: "initial_memory" | "initial_png" | "full_top";
  elapsedMs?: number;
  /** High-level visit stage wall times; capture_timing separately breaks down viewport work. */
  timings?: Record<string, number>;
  error?: string;
  /** Per-size progress belongs to the visit until the complete responsive bundle is committed. */
  viewportResults?: { viewport: string; status: "captured" | "failed"; elapsedMs: number; error?: string;
    timings?: Record<string, number>;
    captureCounts?: { attempts: number; domExtractions: number; initialScreenshots: number; fullScreenshots: number } }[];
}

/** One source's persistent homepage inventory and bounded selection history. */
export interface SiteDiscovery {
  sourceId: string;
  entryUrl: string;
  status: "pending" | "running" | "completed" | "partial" | "failed";
  candidates: LinkCandidate[];
  inventoryComplete: boolean;
  visits: DiscoveryVisit[];
  acceptedPageIds: string[];
  menuEvents: { selector: string; status: string; error?: string }[];
  stopReason?: string;
  error?: string;
}
