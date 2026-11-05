"""Versioned page index shared by filtering, postprocessing and chunk packaging.

The index is the only hand-off between stages. Source images and published hierarchies
stay in their original run directories. Postprocessing adds a separate hierarchy tree
and points the affected viewport records at it; no stage rewrites an input artifact.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT = SCRIPTS.parent
for _path in (PROJECT, SCRIPTS / "webdataset"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from export_pages import find_pages
from provenance import read_provenance


INDEX_SCHEMA = "responsive_ui_pipeline_index_v1"
IMAGE_NAMES = ("full.png", "initial.png")
STAGES = ("source", "filtered", "postprocessed", "final")
PAGE_STATUSES = ("pending", "valid", "rejected", "pending_postprocess", "pending_final_filter")
VIEWPORT_RE = re.compile(r"[A-Za-z0-9._-]+")


def sha256_file(path):
    """Hash a file without loading it into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_relative(value, field):
    """Index paths are POSIX paths relative to a declared root; never absolute or escaping."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative POSIX path")
    if "\\" in value:
        raise ValueError(f"{field} must use POSIX separators: {value}")
    path = Path(value)
    if path.is_absolute() or value.startswith("/") or ".." in path.parts:
        raise ValueError(f"{field} must stay inside its root: {value}")
    return value


def validate_viewports(viewports):
    """Return a deterministic, duplicate-free list of safe viewport names."""
    values = list(viewports)
    if not values:
        raise ValueError("At least one viewport is required")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate viewport names")
    for viewport in values:
        if not isinstance(viewport, str) or not VIEWPORT_RE.fullmatch(viewport):
            raise ValueError(f"Unsafe viewport name: {viewport!r}")
    return values


def resolve_inputs(inputs):
    """Resolve one or more run directories and reject duplicates or nesting."""
    supplied = [inputs] if isinstance(inputs, (str, Path)) else list(inputs)
    if not supplied:
        raise ValueError("At least one input directory is required")
    roots, seen = [], set()
    for value in supplied:
        root = Path(value).resolve()
        if not root.is_dir():
            raise ValueError(f"Input does not exist: {root}")
        if root in seen:
            raise ValueError(f"Duplicate input directory: {root}")
        seen.add(root)
        roots.append(root)
    for position, first in enumerate(roots):
        for second in roots[position + 1:]:
            if first in second.parents or second in first.parents:
                raise ValueError("Input directories must not contain each other")
    return roots


def resolve_path(root, relative, field="path"):
    """Resolve an indexed relative path and require it to stay inside the declared root."""
    _require_relative(relative, field)
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"{field} escapes its root: {relative}")
    return path


def _label_metadata(hierarchy, meta, label):
    """Join index provenance with the saved hierarchy/nodes.json URL fallbacks."""
    url = meta.get("url")
    title = meta.get("title")
    url_source = meta.get("url_source")
    if not url and isinstance(label, dict):
        capture = label.get("capture", {})
        if isinstance(capture, dict) and capture.get("url"):
            url = capture["url"]
            url_source = "hierarchy.json"
    if not url:
        nodes = hierarchy.with_name("nodes.json")
        if nodes.is_file():
            try:
                raw = json.loads(nodes.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            url = raw.get("url")
            title = title or raw.get("title")
            if url:
                url_source = "nodes.json"
    return {"url": url, "entry_url": meta.get("entry_url"), "title": title,
            "category": meta.get("category"), "category_path": meta.get("category_path"),
            "url_source": url_source or ("missing" if not url else "index")}


def _label_paths(hierarchy, source):
    """Read capture.image and return (relative image, relative label, label, error)."""
    relative_hierarchy = hierarchy.relative_to(source).as_posix()
    try:
        label = json.loads(hierarchy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, relative_hierarchy, None, f"invalid hierarchy JSON: {error}"
    if not isinstance(label, dict):
        return None, relative_hierarchy, None, "hierarchy must be a JSON object"
    capture = label.get("capture", {})
    name = capture.get("image", "initial.png") if isinstance(capture, dict) else None
    if name not in IMAGE_NAMES:
        return None, relative_hierarchy, label, f"unsupported capture.image: {name!r}"
    image = hierarchy.with_name(name)
    if not image.is_file():
        return None, relative_hierarchy, label, f"missing {name}: {image.parent}"
    return image.relative_to(source).as_posix(), relative_hierarchy, label, None


def _published_labels(source, records, authoritative):
    """List actual hierarchy files without inventing viewport paths."""
    if authoritative:
        return sorted(path for path in records if path.is_relative_to(source))
    labels = []
    for pages in find_pages(source):
        if any(part.startswith(".") for part in pages.relative_to(source).parts):
            continue
        for hierarchy in pages.rglob("hierarchy.json"):
            if hierarchy.is_symlink():
                continue
            if any(part.startswith(".") for part in hierarchy.relative_to(source).parts):
                continue
            labels.append(hierarchy.resolve())
    return labels


def _inspect_label(source, run_id, hierarchy, records):
    """Read one published label independently so discovery can run in parallel."""
    hierarchy = Path(hierarchy)
    if not hierarchy.is_relative_to(source):
        raise ValueError(f"Hierarchy outside input root: {hierarchy}")
    parent = hierarchy.parent.parent
    relative_parent = parent.relative_to(source).as_posix()
    meta = records.get(hierarchy.resolve(), {})
    viewport = meta.get("viewport") or hierarchy.parent.name
    if not isinstance(viewport, str) or not VIEWPORT_RE.fullmatch(viewport):
        raise ValueError(f"Unsafe viewport name: {viewport!r} ({hierarchy})")
    if hierarchy.is_file():
        image, relative_hierarchy, label, error = _label_paths(hierarchy, source)
        hierarchy_hash = sha256_file(hierarchy)
    else:
        image, relative_hierarchy, label = None, hierarchy.relative_to(source).as_posix(), None
        error, hierarchy_hash = "missing hierarchy.json", None
    page_id = meta.get("page_id") or parent.name
    record = {
        "image": image,
        "original_hierarchy": relative_hierarchy,
        "hierarchy": relative_hierarchy,
        "hierarchy_side": "original",
        "hierarchy_version": label.get("rule_version") if isinstance(label, dict) else None,
        "hierarchy_sha256": hierarchy_hash,
    }
    if error:
        record["error"] = error
    return {
        "parent": parent,
        "relative_parent": relative_parent,
        "viewport": viewport,
        "page_id": page_id,
        "source_id": meta.get("source_id"),
        "run_id": run_id,
        "metadata": _label_metadata(hierarchy, meta, label),
        "record": record,
    }


def build_index(inputs, viewports, progress=False, workers=1):
    """Build a source index from one or more published collection roots.

    Curlie roots with an authoritative collection index contribute exactly the pages
    listed there; other roots are scanned for published image/hierarchy pairs. Every
    discovered viewport keeps the original image path and hierarchy path.
    """
    viewports = validate_viewports(viewports)
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    roots = resolve_inputs(inputs)
    index = {
        "schema": INDEX_SCHEMA,
        "stage": "source",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parent": None,
        "required_viewports": viewports,
        "runs": [],
        "hierarchies_root": None,
        "pages": [],
    }
    for number, source in enumerate(roots):
        run_id = f"run-{number:03d}"
        records, authoritative = read_provenance(source)
        labels = _published_labels(source, records, authoritative)
        if progress:
            print(json.dumps({"type": "pipeline_index_progress", "phase": "start",
                              "run_id": run_id, "root": str(source),
                              "labels": len(labels), "authoritative": authoritative},
                             ensure_ascii=False), flush=True)
        interval = max(1, len(labels) // 20)
        grouped = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for position, item in enumerate(pool.map(
                    lambda hierarchy: _inspect_label(source, run_id, hierarchy, records), labels), 1):
                parent = item["parent"]
                viewport = item["viewport"]
                relative_parent = item["relative_parent"]
                page = grouped.setdefault(parent, {
                    "page_id": item["page_id"],
                    "source_id": item["source_id"],
                    "run_id": run_id,
                    "page_directory": relative_parent,
                    "viewports": {},
                })
                page.update(item["metadata"])
                if viewport in page["viewports"]:
                    raise ValueError(f"Duplicate viewport {viewport!r} for page {relative_parent}")
                page["viewports"][viewport] = item["record"]
                if progress and (position == len(labels)
                                 or (len(labels) > 20 and position % interval == 0)):
                    print(json.dumps({"type": "pipeline_index_progress", "phase": "running",
                                      "run_id": run_id, "checked": position, "total": len(labels),
                                      "percent": round(position * 100 / len(labels), 1),
                                      "pages": len(grouped)}, ensure_ascii=False), flush=True)
        if progress:
            print(json.dumps({"type": "pipeline_index_progress", "phase": "complete",
                              "run_id": run_id, "labels": len(labels),
                              "pages": len(grouped)}, ensure_ascii=False), flush=True)
        for page in grouped.values():
            page["viewports"] = dict(sorted(page["viewports"].items()))
            page["status"] = "pending"
            page["reasons"] = []
        index["pages"].extend(grouped[path] for path in sorted(grouped))
        index["runs"].append({"id": run_id, "root": str(source)})
    index["pages"].sort(key=lambda page: (page["run_id"], page["page_directory"]))
    if not index["pages"]:
        raise ValueError("No published hierarchy.json pages found")
    validate_index(index)
    return index


def validate_index(index):
    """Validate the schema and every path before a stage trusts the index."""
    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        raise ValueError(f"Not a {INDEX_SCHEMA} index")
    if index.get("stage") not in STAGES:
        raise ValueError(f"Unknown index stage: {index.get('stage')!r}")
    validate_viewports(index.get("required_viewports", []))
    runs = index.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("Index runs must be a non-empty array")
    run_ids = set()
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("id"), str) or not run["id"]:
            raise ValueError("Each run needs a non-empty id")
        if run["id"] in run_ids:
            raise ValueError(f"Duplicate run id: {run['id']}")
        run_ids.add(run["id"])
        if not isinstance(run.get("root"), str) or not run["root"]:
            raise ValueError(f"Run {run['id']} needs a root")
    derived = index.get("hierarchies_root")
    if derived is not None and (not isinstance(derived, str) or not derived):
        raise ValueError("hierarchies_root must be null or a path")
    pages = index.get("pages")
    if not isinstance(pages, list):
        raise ValueError("Index pages must be an array")
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("Each page must be an object")
        if page.get("run_id") not in run_ids:
            raise ValueError(f"Page references unknown run: {page.get('run_id')}")
        _require_relative(page.get("page_directory"), "page_directory")
        if page.get("status") not in PAGE_STATUSES:
            raise ValueError(f"Unknown page status: {page.get('status')!r}")
        if not isinstance(page.get("viewports"), dict) or not page["viewports"]:
            raise ValueError(f"Page {page.get('page_id')} has no viewports")
        for viewport, record in page["viewports"].items():
            if not VIEWPORT_RE.fullmatch(viewport):
                raise ValueError(f"Unsafe viewport name: {viewport!r}")
            if not isinstance(record, dict):
                raise ValueError(f"Viewport {viewport} record must be an object")
            if record.get("image") is not None:
                _require_relative(record["image"], f"{viewport}.image")
            _require_relative(record.get("original_hierarchy"), f"{viewport}.original_hierarchy")
            _require_relative(record.get("hierarchy"), f"{viewport}.hierarchy")
            side = record.get("hierarchy_side")
            if side not in ("original", "derived"):
                raise ValueError(f"Viewport {viewport} has invalid hierarchy_side: {side!r}")
            if side == "derived" and not derived:
                raise ValueError(f"Derived hierarchy for {viewport} needs hierarchies_root")
            digest = record.get("hierarchy_sha256")
            if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
                raise ValueError(f"Invalid hierarchy_sha256 for {viewport}")
            for key in ("filter", "postprocess"):
                if key in record and not isinstance(record[key], dict):
                    raise ValueError(f"Viewport {viewport} {key} must be an object")
    return index


def load_index(path):
    """Read and validate an index; malformed indexes never silently pass through."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_index(data)
    return data


def write_index(path, index, overwrite=False):
    """Atomically publish an index; completed stage indexes are not overwritten by default."""
    validate_index(index)
    path = Path(path).resolve()
    if path.exists() and not overwrite:
        raise ValueError(f"Index output exists; choose a new file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(index, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path


def parent_record(path):
    """Return the lineage pointer for a consumed index file."""
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256_file(path)}


def run_root(index, page):
    """Resolve the original run root declared by the index."""
    for run in index["runs"]:
        if run["id"] == page["run_id"]:
            return Path(run["root"]).resolve()
    raise ValueError(f"Page references unknown run: {page.get('run_id')}")


def hierarchy_path(index, page, viewport):
    """Resolve the currently selected hierarchy for one page viewport."""
    record = page["viewports"][viewport]
    base = index.get("hierarchies_root") if record.get("hierarchy_side") == "derived" else run_root(index, page)
    if record.get("hierarchy_side") == "derived" and not base:
        raise ValueError("Derived hierarchy requested without hierarchies_root")
    return resolve_path(base, record["hierarchy"], f"{page.get('page_id')}.{viewport}.hierarchy")


def image_path(index, page, viewport):
    """Resolve the original screenshot declared by one viewport record."""
    record = page["viewports"][viewport]
    if not record.get("image"):
        raise ValueError(f"Missing image path for {page.get('page_id')}.{viewport}")
    return resolve_path(run_root(index, page), record["image"], f"{page.get('page_id')}.{viewport}.image")


def page_reasons(page, viewports):
    """Collect page-level reasons from the required viewport checks."""
    reasons = []
    for viewport in viewports:
        record = page["viewports"].get(viewport)
        if record is None:
            reasons.append(f"{viewport}:missing_viewport")
            continue
        check = record.get("filter", {})
        if not check.get("valid"):
            reasons.extend(f"{viewport}:{reason}" for reason in (check.get("reasons") or ["invalid"]))
    return reasons
