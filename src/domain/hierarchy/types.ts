import type { RawSnapshot } from "../types.js";
import type { StructureLabel } from "../structure/index.js";

/** Depth is computed from retained parent links; kinds describe granularity, not DOM depth. */
export interface VisualNode extends StructureLabel {
  node_id: string;
  parent_id: string | null;
  layer: number;
  kind: "region" | "group" | "element";
  semantic_hint: string;
  tag: string;
  selector: string;
  bbox: [number, number, number, number];
  /** Original unmodified DOM layout geometry; bbox may instead describe owned visual content. */
  layout_bbox: [number, number, number, number];
  /** Function remains a link/button even when its visual content is an image. */
  interaction_role: string | null;
  /** Original line fragments expose low-occupancy unions without splitting one DOM identity. */
  fragments: [number, number, number, number][];
  geometry_warnings: string[];
  source_dom_parent_id: string | null;
  reasons: string[];
  /** Original classification priority, retained for faithful offline geometry refinement. */
  selection_priority?: number;
}

/** Every visible raw node remains traceable, including wrappers and atomic descendants. */
export interface HierarchyDecision {
  node_id: string;
  status: "retained" | "excluded";
  reason: string;
  represented_by: string | null;
}

/** Rule-generated candidate labels, never silently promoted to human ground truth. */
export interface VisualHierarchy {
  schema: "visual_hierarchy_candidate_v1";
  rule_version: string;
  structure_rule_version: string;
  annotation_status: "rule_generated_unverified";
  parent_basis: "nearest_retained_dom_ancestor" | "retained_dom_ancestor_with_sibling_absorption";
  /** Child-generation thresholds recorded so exported labels retain their rule provenance. */
  geometry_policy?: { siblingIou: number; parentSizeTolerance: number };
  capture: Pick<RawSnapshot, "documentId" | "capturedAt"> & {
    image: string; width: number; height: number;
    /** Actual captured page URL, when retained in the source metadata. */
    url?: string;
  };
  nodes: VisualNode[];
  /** Excluded foreground candidates must not silently become confirmed background negatives. */
  ignore_regions: { node_id: string; reason: string; bbox: [number, number, number, number] }[];
  decisions: HierarchyDecision[];
  stats: {
    raw_visible: number; retained: number; excluded: number; roots: number;
    max_layer: number; by_layer: Record<string, number>; by_kind: Record<string, number>;
    by_struct: Record<string, number>; by_disposition: Record<string, number>;
    visible_after_clipping: number; ancestor_clipped_out: number; ancestor_clipped_partial: number;
    area_filtered_out: number;
    css_hidden_out: number;
    screen_reader_only_out: number;
  };
}
