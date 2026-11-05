"""Index-driven hierarchy postprocessing: derived clean labels, original screenshots."""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
LAYER = SCRIPTS / "layer_postprocess"
for _path in (SCRIPTS, LAYER):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from grouping_pipeline import process as default_process, PIPELINE_VERSION
from clean_export import clean_hierarchy
from storage import write

from . import index as pipeline_index


def _prepare_output(path, label, resume):
    """Require a separate output tree; only resume may reuse a populated directory."""
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()) and not resume:
        raise ValueError(f"{label} must be empty; choose a new directory: {path}")
    return path


def _check_separation(paths):
    """Derived roots and processing reports must not contain each other."""
    resolved = [Path(path).resolve() for path in paths]
    for position, first in enumerate(resolved):
        for second in resolved[position + 1:]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("Output directories must be distinct and not nested")


def _observations(index, pages, viewports):
    """Turn accepted page records into one observation per required viewport."""
    items = []
    for page in pages:
        run_root = pipeline_index.run_root(index, page)
        for viewport in viewports:
            record = page["viewports"].get(viewport)
            if record is None or not record.get("filter", {}).get("valid"):
                raise ValueError(f"Page {page.get('page_id')} viewport {viewport} is not filter-valid")
            relative = Path(page["run_id"]) / Path(page["page_directory"]) / viewport
            items.append({
                "page": page,
                "viewport": viewport,
                "relative": relative,
                "relative_file": relative / "hierarchy.json",
                "run_root": run_root,
                "hierarchy_file": pipeline_index.hierarchy_path(index, page, viewport),
                "image": pipeline_index.image_path(index, page, viewport),
            })
    return items


def _existing_result(item, processing_output, derived_root, resume):
    """Reuse an accepted same-version audit and derived hierarchy without an API call."""
    if not resume:
        return None
    relative = item["relative"]
    audit_file = processing_output / relative / "layer-review.json"
    derived_file = derived_root / relative / "hierarchy.json"
    if not audit_file.is_file() or not derived_file.is_file():
        return None
    try:
        audit = json.loads(audit_file.read_text(encoding="utf-8"))
        if audit.get("status") != "accepted" or audit.get("postprocess_version") != PIPELINE_VERSION:
            return None
        if Path(audit.get("source", "")).resolve() != item["hierarchy_file"].resolve():
            return None
        clean = json.loads(derived_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return {"status": "accepted", "resumed": True,
            "hierarchy_version": clean.get("rule_version", PIPELINE_VERSION),
            "hierarchy_sha256": pipeline_index.sha256_file(derived_file)}


def _run_one(item, processor, config, processing_output, derived_root, resume):
    """Process one hierarchy and publish its clean L1-L5 label under the derived root."""
    reused = _existing_result(item, processing_output, derived_root, resume)
    if reused:
        return {**item, **reused}
    try:
        result = processor(
            item["hierarchy_file"], item["run_root"], processing_output, config, False,
            None, image_override=item["image"], relative_override=item["relative_file"],
            copy_image=False)
    except Exception as error:
        return {**item, "status": "failed", "error": f"{type(error).__name__}: {error}"}
    status = result.get("status")
    if status != "accepted":
        return {**item, "status": status or "failed",
                "error": result.get("error") or result.get("review_blockers")}
    try:
        fused = processing_output / item["relative"] / "fused-hierarchy.json"
        tree = json.loads(fused.read_text(encoding="utf-8"))
        clean = clean_hierarchy(tree)
        destination = derived_root / item["relative"] / "hierarchy.json"
        write(destination, clean)
        return {**item, "status": "accepted",
                "hierarchy_version": clean.get("rule_version", PIPELINE_VERSION),
                "hierarchy_sha256": pipeline_index.sha256_file(destination)}
    except Exception as error:
        return {**item, "status": "failed", "error": f"{type(error).__name__}: {error}"}


def run_index(index_path, index_output, hierarchy_root, processing_output, config,
              limit=0, concurrency=None, resume=False, progress=False, processor=None):
    """Run postprocessing from a filtered index and return the updated index.

    Only pages marked valid by the filter stage are processed. Screenshots stay in the
    original run roots; the derived tree contains hierarchy labels and reports only.
    ``resume=True`` reuses accepted same-version derived outputs and retries the rest.
    """
    index = pipeline_index.load_index(index_path)
    if config.get("pixels", {}).get("pixel_fallback_enabled", True):
        try:
            import PIL  # noqa: F401
            import numpy  # noqa: F401
        except ModuleNotFoundError as error:
            raise ValueError(
                "Pixel fallback needs Pillow and NumPy: install "
                "scripts/layer_postprocess/requirements-pixels.txt, or set "
                "pixels.pixel_fallback_enabled=false") from error
    roots = [Path(run["root"]).resolve() for run in index["runs"]]
    derived_root = Path(hierarchy_root).resolve()
    reports_root = Path(processing_output).resolve()
    _check_separation([*roots, derived_root, reports_root])
    derived_root = _prepare_output(derived_root, "Hierarchy output", resume)
    reports_root = _prepare_output(reports_root, "Processing output", resume)
    index_output = Path(index_output).resolve()
    if index_output.exists() and not resume:
        raise ValueError(f"Index output exists; choose a new file: {index_output}")
    if index_output in derived_root.parents or derived_root in index_output.parents \
            or index_output in reports_root.parents or reports_root in index_output.parents:
        raise ValueError("Index output must be separate from generated trees")
    viewports = list(index["required_viewports"])
    eligible = [page for page in index["pages"] if page["status"] == "valid"]
    if limit:
        if limit < 0:
            raise ValueError("limit must be nonnegative")
        eligible = eligible[:limit]
    selected_keys = {(page["run_id"], page["page_directory"]) for page in eligible}
    for page in index["pages"]:
        if page["status"] == "valid" and (page["run_id"], page["page_directory"]) not in selected_keys:
            page["status"] = "pending_postprocess"
            page["reasons"] = ["postprocess_limit_reached"]
    if not eligible:
        raise ValueError("No filter-valid pages to postprocess")
    try:
        items = _observations(index, eligible, viewports)
    except ValueError as error:
        raise ValueError(f"Cannot postprocess index: {error}") from error
    if concurrency is None:
        concurrency = config.get("concurrency", 1)
    if type(concurrency) is not int or not 1 <= concurrency <= 256:
        raise ValueError("concurrency must be an integer in 1..256")
    runner = processor or default_process
    started = time.monotonic()
    last_beat = 0.0
    results = []
    checked = 0
    statuses = {}
    if progress:
        print(json.dumps({"type": "postprocess_progress", "phase": "start",
                          "pages": len(eligible), "observations": len(items),
                          "concurrency": concurrency}, ensure_ascii=False), flush=True)
    with ThreadPoolExecutor(max_workers=concurrency) as workers:
        futures = [workers.submit(_run_one, item, runner, config, reports_root, derived_root, resume)
                   for item in items]
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=30)
            for future in done:
                result = future.result()
                results.append(result)
                checked += 1
                statuses[result["status"]] = statuses.get(result["status"], 0) + 1
            elapsed = time.monotonic() - started
            if progress and (elapsed - last_beat >= 30 or not pending):
                print(json.dumps({"type": "postprocess_progress", "phase": "running",
                                  "checked": checked, "total": len(items),
                                  "in_flight": len(pending),
                                  "percent": round(checked * 100 / len(items), 1),
                                  "statuses": statuses,
                                  "seconds": round(elapsed, 1)},
                                 ensure_ascii=False), flush=True)
                last_beat = elapsed
    for result in results:
        page = result["page"]
        viewport = result["viewport"]
        record = page["viewports"][viewport]
        if result["status"] == "accepted":
            relative_hierarchy = (result["relative"] / "hierarchy.json").as_posix()
            record["hierarchy_side"] = "derived"
            record["hierarchy"] = relative_hierarchy
            record["hierarchy_version"] = result["hierarchy_version"]
            record["hierarchy_sha256"] = result["hierarchy_sha256"]
            record["postprocess"] = {"status": "accepted", "version": PIPELINE_VERSION,
                                     "resumed": bool(result.get("resumed"))}
        else:
            record["postprocess"] = {"status": result.get("status", "failed"),
                                     "version": PIPELINE_VERSION,
                                     "error": result.get("error")}
    for page in eligible:
        failures = []
        for viewport in viewports:
            record = page["viewports"].get(viewport)
            status = record.get("postprocess", {}).get("status") if record else "missing"
            if status != "accepted" or record.get("hierarchy_side") != "derived":
                failures.append(f"{viewport}:postprocess_{status or 'missing'}")
        page["status"] = "pending_final_filter" if not failures else "rejected"
        page["reasons"] = failures
    index["stage"] = "postprocessed"
    index["hierarchies_root"] = str(derived_root)
    index["processing_root"] = str(reports_root)
    index["parent"] = pipeline_index.parent_record(index_path)
    index["created_at"] = datetime.now(timezone.utc).isoformat()
    counts = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    summary = {
        "type": "postprocess_completed",
        "pages_selected": len(eligible),
        "pages_skipped": len(index["pages"]) - len(eligible),
        "observations": len(items),
        "statuses": counts,
        "seconds": round(time.monotonic() - started, 3),
        "hierarchies_root": str(derived_root),
        "processing_root": str(reports_root),
    }
    return index, summary
