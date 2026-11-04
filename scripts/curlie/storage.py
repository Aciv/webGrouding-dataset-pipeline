"""Atomic checkpoints and training-pair publication; reuse the existing export contract."""
import json
import os
import shutil
import re
from pathlib import Path
from export_pages import training_files


def read_json(file, default=None):
    """Missing checkpoints are allowed; malformed checkpoints remain explicit errors."""
    try:
        return json.loads(Path(file).read_text())
    except FileNotFoundError:
        return default


def save_json(file, value):
    """Replace a checkpoint only after the complete new JSON has been flushed."""
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(file.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(file)


def identify_annotation_version(output, page):
    """Backfill version metadata in old indices by reading labels; never change files."""
    if page.get("annotation_rules") and page.get("annotation_rule_version"):
        return
    versions = {}
    for viewport, observation in page["viewports"].items():
        label = read_json(output / observation["hierarchy"])
        versions[viewport] = label.get("rule_version", "unversioned")
    unique = set(versions.values())
    page["annotation_rules"] = versions
    page["annotation_rule_version"] = next(iter(unique)) if len(unique) == 1 else "mixed"


def publish_batch(work, output, candidates, viewports, max_pages, source_ids=None):
    """Commit only all-viewport pages; each final viewport contains exactly two files.

    Retry after a crash accepts an already published pair only when capture identity
    and image size agree with the still-retained batch. No source is deleted here.
    """
    index = read_json(work / "batch-index.json", {})
    discovery = read_json(work / "discovery-index.json", {})
    by_id = {item["id"]: item for item in candidates}
    records = []
    for page in index.get("pages", []):
        if source_ids is not None and page["source"] not in source_ids:
            continue
        if len(records) >= max_pages:
            break
        if page["status"] != "captured":
            continue
        observations = {o["viewport"]: o for o in page["observations"] if o["status"] == "captured"}
        if not all(viewport in observations for viewport in viewports):
            continue
        labels = {}
        for viewport in viewports:
            folder = work / observations[viewport]["directory"]
            labels[viewport] = read_json(folder / "hierarchy.json")
        versions = {viewport: label.get("rule_version", "unversioned") for viewport, label in labels.items()}
        if any(not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", version) for version in versions.values()):
            raise ValueError(f"Invalid annotation version: {page['id']}")
        unique_versions = set(versions.values())
        version = next(iter(unique_versions)) if len(unique_versions) == 1 else "mixed"
        relative_page = Path("versions") / version / "pages" / page["id"]
        # An old process may have published a pair just before its checkpoint was
        # interrupted. Reuse that exact legacy path rather than duplicating it.
        if (output / "pages" / page["id"]).exists():
            relative_page = Path("pages") / page["id"]
        destination = output / relative_page
        staging = output / ".publishing" / page["id"]
        shutil.rmtree(staging, ignore_errors=True)
        paths = {}
        for viewport in viewports:
            folder = work / observations[viewport]["directory"]
            files = training_files(folder / "hierarchy.json")
            target = staging / viewport
            target.mkdir(parents=True, exist_ok=True)
            for file in files:
                shutil.copy2(file, target / file.name)
            paths[viewport] = {"image": (relative_page / viewport / files[0].name).as_posix(),
                               "hierarchy": (relative_page / viewport / "hierarchy.json").as_posix()}
        if destination.exists():
            for viewport in viewports:
                old = training_files(destination / viewport / "hierarchy.json")
                new = training_files(staging / viewport / "hierarchy.json")
                if old[1].read_bytes() != new[1].read_bytes() or old[0].stat().st_size != new[0].stat().st_size:
                    raise ValueError(f"Conflicting published page: {page['id']}")
            shutil.rmtree(staging)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(destination)
        source = by_id[page["source"]]
        records.append({"id": page["id"], "source": page["source"], "url": page["url"],
                        "title": page.get("title"), "host": source["host"],
                        "category": source["category"], "category_path": source["category_path"],
                        "annotation_rule_version": version, "annotation_rules": versions,
                        "capture_metadata": {viewport: {key: observation[key] for key in
                            ("elapsedMs", "nodes", "visibleCandidates", "stable", "warnings", "captureIdentity", "timings", "captureCounts")
                            if key in observation} for viewport, observation in observations.items()},
                        "viewports": paths})
    outcomes = [{"id": site["sourceId"], "status": site["status"],
                 "capturedPages": len(site["acceptedPageIds"]),
                 "exportedPages": sum(page["source"] == site["sourceId"] for page in records), "error": site.get("error")}
                for site in discovery.get("sites", [])
                if source_ids is None or site["sourceId"] in source_ids]
    return records, outcomes
