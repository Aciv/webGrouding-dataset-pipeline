"""Geometry-only page screening; no browser, model calls or annotation mutation."""
import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Rules:
    """Thresholds are per image; bbox geometry is [x,y,width,height] in image CSS pixels."""
    min_boxes: int = 10
    min_outside_boxes: int = 5
    max_outside_ratio: float = 0.2
    tolerance_px: float = 2.0
    check_layout_overflow: bool = True
    min_layout_overflow_ratio: float = 0.1
    layout_overflow_policy: str = "reject"

    def __post_init__(self):
        if self.layout_overflow_policy not in ("reject", "warn", "ignore"):
            raise ValueError("layout_overflow_policy must be reject, warn or ignore")

    def to_json(self):
        """Serializable rule version and explicit thresholds for reproducibility."""
        return {"version": "valid_responsive_pages_v1", **asdict(self)}


def finite_number(value):
    """Booleans are not valid coordinates despite being Python integers."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def box_values(value):
    """Reject malformed and zero/negative-size boxes rather than silently repairing labels."""
    if not isinstance(value, list) or len(value) != 4 or not all(finite_number(n) for n in value):
        return None
    x, y, width, height = value
    return (x, y, width, height) if width > 0 and height > 0 else None


def inspect_geometry(label, rules):
    """Count training-box violations plus substantial original horizontal clipping separately.

    Original layout extending below a bounded full screenshot is not a responsive failure.
    Layout overflow is a heuristic: saved labels cannot prove whether a carousel was intentional.
    """
    if not isinstance(label, dict):
        raise ValueError("hierarchy must be a JSON object")
    capture = label.get("capture", {})
    width, height = capture.get("width"), capture.get("height")
    if not all(finite_number(n) and n > 0 for n in (width, height)):
        raise ValueError("capture.width/height must be positive finite numbers")
    nodes = label.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("nodes must be an array")
    reasons, samples = [], []
    invalid, outside, horizontal, vertical, layout_horizontal = 0, 0, 0, 0, 0
    layout_missing, layout_malformed = 0, 0
    bad_union = 0
    reject_layout = rules.check_layout_overflow and rules.layout_overflow_policy == "reject"
    warn_layout = rules.check_layout_overflow and rules.layout_overflow_policy == "warn"
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            invalid += 1
            if len(samples) < 5:
                samples.append({"node_id": str(index), "reason": "invalid_node"})
            continue
        box = box_values(node.get("bbox"))
        if box is None:
            invalid += 1
            if len(samples) < 5:
                samples.append({"node_id": node.get("node_id", str(index)), "reason": "invalid_bbox"})
            continue
        x, y, w, h = box
        hx = x < -rules.tolerance_px or x + w > width + rules.tolerance_px
        vy = y < -rules.tolerance_px or y + h > height + rules.tolerance_px
        horizontal += int(hx)
        vertical += int(vy)
        outside += int(hx or vy)
        layout = box_values(node.get("layout_bbox"))
        lx = False
        if "layout_bbox" not in node:
            layout_missing += 1
        elif layout is None:
            layout_malformed += 1
        else:
            left, _top, lw, _lh = layout
            overflow = max(0, -left) + max(0, left + lw - width)
            lx = overflow > rules.tolerance_px and overflow / lw >= rules.min_layout_overflow_ratio
            layout_horizontal += int(lx)
        flagged = hx or vy or reject_layout and lx
        bad_union += int(flagged)
        if flagged and len(samples) < 5:
            samples.append({"node_id": node.get("node_id", str(index)), "bbox": node["bbox"],
                            "layout_bbox": node.get("layout_bbox") if layout is not None else None, "training_outside": hx or vy,
                            "layout_horizontal_overflow": lx})
    count = len(nodes)
    ratio = bad_union / count if count else 0
    if count < rules.min_boxes:
        reasons.append("too_few_boxes")
    if invalid:
        reasons.append("invalid_bbox_values")
    if layout_malformed and reject_layout:
        reasons.append("invalid_layout_bbox_values")
    if bad_union >= rules.min_outside_boxes and ratio >= rules.max_outside_ratio:
        reasons.append("excessive_out_of_bounds")
    return {"valid": not reasons, "reasons": reasons, "bbox_count": count,
            "width": width, "height": height, "invalid_boxes": invalid,
            "training_outside_boxes": outside, "training_horizontal_outside": horizontal,
            "training_vertical_outside": vertical, "layout_horizontal_outside": layout_horizontal,
            "layout_missing": layout_missing, "layout_malformed": layout_malformed,
            "layout_warnings": layout_horizontal if warn_layout else 0,
            "outside_boxes": bad_union, "outside_ratio": ratio, "samples": samples}
