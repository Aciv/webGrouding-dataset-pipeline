/** Human issue geometry in original image CSS pixels, never scaled preview coordinates. */
export type ReviewBox = [number, number, number, number];

/** Capture identity binds feedback to one DOM/image observation. */
export interface CaptureReference {
  documentId: string;
  capturedAt: string;
  image: string;
  width: number;
  height: number;
}

/** Persisted issue; DOM evidence is resolved by the server from the original snapshot. */
export interface ReviewIssue {
  id: string;
  kind: "existing_box" | "missing_box";
  issueType: string;
  reason: string;
  content: string;
  expectedTag: string;
  bbox: ReviewBox;
  nodeId?: string;
  tag?: string;
  selector?: string;
  createdAt: string;
  updatedAt: string;
}

/** One observation's feedback, stored beside its screenshot. */
export interface HumanReview {
  schema: "dom_bbox_human_review_v1";
  pageId: string;
  viewportId: string;
  capture: CaptureReference;
  coordinateSystem: "image_css_pixels";
  revision: number;
  reviewed: boolean;
  issues: ReviewIssue[];
  updatedAt: string | null;
}
