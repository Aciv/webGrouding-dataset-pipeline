"""Read-only screening CLI. Output manifests point to original image/label pairs."""
import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

from .inventory import page_inventory, png_size, training_files
from .rules import Rules, inspect_geometry


def inspect_page(page, viewports, rules):
    """A page is valid only if every requested resolution has a usable training pair."""
    root = Path(page["input_root"])
    result = {key: value for key, value in page.items() if key != "labels"}
    result.update(valid=True, reasons=[], viewports={})
    for viewport in viewports:
        label_file = page["labels"][viewport]
        observation = {"hierarchy": label_file.relative_to(root).as_posix()}
        try:
            label = json.loads(label_file.read_text(encoding="utf-8"))
            geometry = inspect_geometry(label, rules)
            image, _ = training_files(label_file)
            if not image.resolve().is_relative_to(root):
                raise ValueError("Image outside input root")
            observation.update(geometry, image=image.relative_to(root).as_posix())
            actual = png_size(image)
            observation["image_size"] = list(actual)
            if tuple(actual) != (geometry["width"], geometry["height"]):
                observation["valid"] = False
                observation["reasons"].append("image_size_mismatch")
        except (OSError, ValueError, TypeError, AttributeError) as error:
            observation.update(valid=False, reasons=["missing_or_invalid_training_pair"], error=str(error))
        result["viewports"][viewport] = observation
        if not observation["valid"]:
            result["valid"] = False
            result["reasons"].extend(f"{viewport}:{reason}" for reason in observation["reasons"])
    return result


def filter_pages(sources, output, rules, viewports, workers=8):
    """Write one atomic manifest; never delete, rewrite or copy training artifacts."""
    output = Path(output).resolve()
    if output.name in ("hierarchy.json", "collection-index.json", "batch-index.json", "discovery-index.json"):
        raise ValueError("Output must not overwrite a source annotation or collection index")
    if output.exists():
        raise ValueError("Output exists; choose a new JSON filename")
    inventory, seen = [], set()
    for source in sources:
        for page in page_inventory(source, viewports):
            key = tuple(str(page["labels"][viewport].resolve()) for viewport in viewports)
            if key not in seen:
                seen.add(key)
                inventory.append(page)
    if not inventory:
        raise ValueError("No published hierarchy.json pages found")
    valid, rejected = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for count, result in enumerate(pool.map(lambda page: inspect_page(page, viewports, rules), inventory), 1):
            (valid if result["valid"] else rejected).append(result)
            if count % 1000 == 0:
                print(json.dumps({"type": "filter_progress", "checked": count, "total": len(inventory),
                                  "valid": len(valid), "rejected": len(rejected)}), flush=True)
    reasons = collections.Counter(reason for page in rejected for reason in set(page["reasons"]))
    categories = collections.Counter(page["category"] or "unknown" for page in valid)
    report = {"schema": "valid_responsive_pages_v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "inputs": [str(Path(source).resolve()) for source in sources],
              "path_base": "each_page.input_root", "required_viewports": viewports,
              "rules": rules.to_json(), "count_scope": "all_nodes_per_viewport",
              "summary": {"pages": len(inventory), "valid_pages": len(valid), "rejected_pages": len(rejected),
                          "valid_images": len(valid) * len(viewports), "reasons": dict(reasons),
                          "valid_categories": dict(categories)},
              "valid_pages": valid, "rejected_pages": rejected}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, prefix=output.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(output)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    print(json.dumps({"type": "filter_completed", **report["summary"], "output": str(output)}, ensure_ascii=False))
    return report


def main():
    """Validate configurable thresholds before scanning files; no collection starts here."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", nargs="+", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--viewports", nargs="+", default=["desktop", "mobile", "tablet"])
    parser.add_argument("--min-boxes", type=int, default=10)
    parser.add_argument("--min-outside-boxes", type=int, default=5)
    parser.add_argument("--max-outside-ratio", type=float, default=0.2)
    parser.add_argument("--tolerance-px", type=float, default=2)
    parser.add_argument("--check-layout-overflow", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--min-layout-overflow-ratio", type=float, default=0.1)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if min(args.min_boxes, args.min_outside_boxes, args.workers) < 1:
        parser.error("Counts and workers must be positive")
    if not math.isfinite(args.tolerance_px) or args.tolerance_px < 0:
        parser.error("tolerance-px must be finite and nonnegative")
    if not all(math.isfinite(n) and 0 < n <= 1 for n in (args.max_outside_ratio, args.min_layout_overflow_ratio)):
        parser.error("Ratios must be in (0,1]")
    if len(set(args.viewports)) != len(args.viewports) or any(not v or "/" in v or "\\" in v or v in (".", "..") for v in args.viewports):
        parser.error("Invalid/duplicate viewport names")
    rules = Rules(args.min_boxes, args.min_outside_boxes, args.max_outside_ratio, args.tolerance_px,
                  args.check_layout_overflow, args.min_layout_overflow_ratio)
    try:
        filter_pages(args.input, args.output, rules, args.viewports, args.workers)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"Filter failed: {error}\n")
