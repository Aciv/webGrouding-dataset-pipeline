import type { RawNode } from "../types.js";

/** Coarse DOM-derived classes; unknown is a review state, not a tenth training class. */
export const STRUCT_TYPES = ["text", "image", "graphic", "media", "control", "table", "list", "region", "block"] as const;
export type StructType = typeof STRUCT_TYPES[number];
export type StructureKind = "region" | "group" | "element";
export type Disposition = "detection" | "structure_only" | "ignore" | "review";

/** Visible-descendant facts reused by independent rules. */
export interface Descendants { controls: number; headings: number; media: number; children: number }

/** Pure snapshot lookup; no browser, filesystem, application or hierarchy dependencies. */
export interface StructureContext {
  visibleIds: ReadonlySet<string>;
  children(node: RawNode): RawNode[];
  ancestors(node: RawNode): RawNode[];
  descendants(node: RawNode): Descendants;
}

/** Classification sidecar, independent of source identity and subsequent geometry processing. */
export interface StructureLabel {
  struct: StructType | "unknown";
  subtype: string;
  kind: StructureKind;
  disposition: Disposition;
  semantic_role: string | null;
  text_role: "heading" | "body" | "label" | "code" | null;
  source_node_ids: string[];
  structure_reasons: string[];
}

/** A null label explicitly rejects a node rather than assigning every wrapper to block. */
export interface StructureDecision {
  label: StructureLabel | null;
  reason: string;
  priority: number;
  atomic: boolean;
}

/** Null means not applicable; the final fallback determines unknown versus excluded. */
export type StructureRule = (node: RawNode, context: StructureContext) => StructureDecision | null;
