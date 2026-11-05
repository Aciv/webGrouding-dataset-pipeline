"""Index-aware geometry filtering for original and postprocessed hierarchies."""
import collections
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from page_filter.inventory import png_size
from page_filter.rules import inspect_geometry

from . import index as pipeline_index


def _filter_block(observation, rules):
    """Keep the per-viewport verdict and the geometry evidence that produced it."""
    return {
        "valid": bool(observation["valid"]),
        "reasons": list(observation["reasons"]),
        "rule_version": rules.to_json()["version"],
        "bbox_count": observation.get("bbox_count"),
        "invalid_boxes": observation.get("invalid_boxes"),
        "training_outside_boxes": observation.get("training_outside_boxes"),
        "training_horizontal_outside": observation.get("training_horizontal_outside"),
        "training_vertical_outside": observation.get("training_vertical_outside"),
        "layout_horizontal_outside": observation.get("layout_horizontal_outside"),
        "layout_warnings": observation.get("layout_warnings", 0),
        "layout_overflow_policy": rules.layout_overflow_policy,
        "layout_missing": observation.get("layout_missing"),
        "layout_malformed": observation.get("layout_malformed"),
        "outside_boxes": observation.get("outside_boxes"),
        "outside_ratio": observation.get("outside_ratio"),
        "image_size": observation.get("image_size"),
        "error": observation.get("error"),
    }


def inspect_viewport(index, page, viewport, rules):
    """Validate one viewport using the currently selected hierarchy and original image."""
    record = page["viewports"].get(viewport)
    if record is None:
        raise ValueError("missing_viewport")
    observation = {
        "hierarchy": record.get("hierarchy"),
        "hierarchy_side": record.get("hierarchy_side"),
        "image": record.get("image"),
    }
    if not record.get("image"):
        raise ValueError(record.get("error") or "missing image")
    if record.get("error"):
        raise ValueError(record["error"])
    hierarchy = pipeline_index.hierarchy_path(index, page, viewport)
    image = pipeline_index.image_path(index, page, viewport)
    blob = hierarchy.read_bytes()
    expected_hash = record.get("hierarchy_sha256")
    if expected_hash:
        actual_hash = hashlib.sha256(blob).hexdigest()
        if actual_hash != expected_hash:
            label = None
            try:
                label = json.loads(blob)
            except (ValueError, UnicodeDecodeError):
                pass
            observation.update(valid=False, reasons=["hierarchy_sha256_mismatch"],
                               error=f"expected {expected_hash}, found {actual_hash}")
            return observation, label
    label = json.loads(blob)
    if not isinstance(label, dict):
        raise ValueError("hierarchy must be a JSON object")
    capture = label.get("capture", {})
    expected_name = capture.get("image", "initial.png") if isinstance(capture, dict) else None
    if expected_name not in pipeline_index.IMAGE_NAMES:
        raise ValueError(f"unsupported capture.image: {expected_name!r}")
    if Path(record["image"]).name != expected_name:
        observation.update(valid=False, reasons=["image_name_mismatch"],
                           error=f"capture.image={expected_name!r} but index image={Path(record['image']).name!r}")
        return observation, label
    geometry = inspect_geometry(label, rules)
    actual = png_size(image)
    observation.update(geometry, image_size=list(actual))
    if tuple(actual) != (geometry["width"], geometry["height"]):
        observation["valid"] = False
        observation["reasons"].append("image_size_mismatch")
    return observation, label


def _filter_page(index, page, rules, viewports, require_derived):
    """Inspect one page; the page is valid only when every required viewport is valid."""
    if require_derived and page.get("status") == "rejected":
        return page
    if require_derived:
        blockers = []
        for viewport in viewports:
            record = page["viewports"].get(viewport)
            if record is None:
                blockers.append(f"{viewport}:missing_viewport")
            elif record.get("hierarchy_side") != "derived":
                blockers.append(f"{viewport}:missing_derived_hierarchy")
            elif record.get("postprocess", {}).get("status") != "accepted":
                status = record.get("postprocess", {}).get("status", "missing")
                blockers.append(f"{viewport}:postprocess_{status}")
        if blockers:
            page["status"] = "rejected"
            page["reasons"] = blockers
            return page
    reasons = []
    warnings = []
    capture_url = None
    for viewport in viewports:
        try:
            observation, label = inspect_viewport(index, page, viewport, rules)
            if not capture_url and isinstance(label, dict):
                capture = label.get("capture", {})
                if isinstance(capture, dict) and capture.get("url"):
                    capture_url = capture["url"]
            if record := page["viewports"].get(viewport):
                record["image_size"] = observation.get("image_size")
                record["filter"] = _filter_block(observation, rules)
            if observation.get("layout_warnings"):
                warnings.append(f"{viewport}:layout_overflow_warning")
            if not observation["valid"]:
                reasons.extend(f"{viewport}:{reason}" for reason in observation["reasons"])
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as error:
            record = page["viewports"].get(viewport)
            if record is None:
                reasons.append(f"{viewport}:missing_viewport")
                continue
            observation = {"valid": False, "reasons": ["missing_or_invalid_training_pair"], "error": str(error)}
            record["filter"] = _filter_block(observation, rules)
            reasons.append(f"{viewport}:missing_or_invalid_training_pair")
    if not page.get("url") and capture_url:
        page["url"] = capture_url
        page["url_source"] = "hierarchy.json"
    page["status"] = "valid" if not reasons else "rejected"
    page["reasons"] = reasons
    page["warnings"] = warnings
    return page


def filter_index(index, rules, viewports=None, workers=8, stage="filtered", progress=False):
    """Filter every page in-place and return (index, summary).

    ``stage='filtered'`` validates the current hierarchy (original or derived).
    ``stage='final'`` additionally requires every required viewport to point at an
    accepted derived hierarchy, so original labels cannot slip into the final index.
    Progress events are JSON lines so long runs remain observable without log parsing.
    """
    if stage not in ("filtered", "final"):
        raise ValueError(f"Unsupported filter stage: {stage}")
    viewports = pipeline_index.validate_viewports(viewports or index["required_viewports"])
    index["required_viewports"] = list(viewports)
    index["stage"] = stage
    index["rules"] = rules.to_json()
    index["created_at"] = datetime.now(timezone.utc).isoformat()
    require_derived = stage == "final"
    if require_derived and not index.get("hierarchies_root"):
        raise ValueError("Final filtering requires a postprocessed index with hierarchies_root")
    pages = index["pages"]
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    total = len(pages)
    checked = valid_count = rejected_count = 0
    interval = max(1, total // 20)
    began = time.monotonic()
    if progress:
        print(json.dumps({"type": "pipeline_filter_progress", "stage": stage, "phase": "start",
                          "checked": 0, "total": total, "valid_pages": 0, "rejected_pages": 0},
                         ensure_ascii=False), flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for page in pool.map(lambda page: _filter_page(index, page, rules, viewports, require_derived), pages):
            checked += 1
            if page["status"] == "valid":
                valid_count += 1
            elif page["status"] == "rejected":
                rejected_count += 1
            if progress and (checked == total or (total > 50 and checked % interval == 0)):
                print(json.dumps({"type": "pipeline_filter_progress", "stage": stage, "phase": "running",
                                  "checked": checked, "total": total,
                                  "percent": round(checked * 100 / total, 1),
                                  "valid_pages": valid_count, "rejected_pages": rejected_count,
                                  "seconds": round(time.monotonic() - began, 1)}, ensure_ascii=False),
                      flush=True)
    valid = [page for page in pages if page["status"] == "valid"]
    rejected = [page for page in pages if page["status"] == "rejected"]
    reasons = collections.Counter(reason for page in rejected for reason in set(page["reasons"]))
    categories = collections.Counter(page.get("category") or "unknown" for page in valid)
    summary = {
        "pages": len(pages),
        "valid_pages": len(valid),
        "rejected_pages": len(rejected),
        "valid_images": len(valid) * len(viewports),
        "reasons": dict(reasons),
        "valid_categories": dict(categories),
        "required_viewports": list(viewports),
        "stage": stage,
    }
    return index, summary
