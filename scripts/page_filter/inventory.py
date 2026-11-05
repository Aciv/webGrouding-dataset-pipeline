"""Reuse published provenance and the canonical training-pair resolver for offline filtering."""
import struct
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from export_pages import find_pages, training_files
from webdataset.provenance import read_provenance


def png_size(image):
    """Read PNG IHDR dimensions without loading a long screenshot or requiring Pillow."""
    with Path(image).open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"Invalid PNG header: {image}")
    size = struct.unpack(">II", header[16:24])
    if min(size) <= 0:
        raise ValueError(f"Invalid PNG dimensions: {image}")
    return size


def page_inventory(source, viewports):
    """Group labels by actual page directory; annotation versions never overwrite each other.

    A top-level Curlie index is authoritative, so in-flight .work pages are excluded.
    Without one, exported pages directories are scanned and available provenance is joined.
    """
    source = Path(source).resolve()
    if not source.is_dir():
        raise ValueError(f"Input directory does not exist: {source}")
    if source.name == "pages" and not any((source.parent / name).is_file() for name in ("collection-index.json", "batch-index.json")):
        # A standalone exported pages folder must not trigger a scan of sibling runtimes/source repos.
        provenance, authoritative = {}, False
    else:
        provenance, authoritative = read_provenance(source)
    labels = []
    if authoritative:
        labels = sorted(label for label in provenance if label.is_relative_to(source))
    else:
        for pages in find_pages(source):
            if any(part.startswith(".") for part in pages.relative_to(source).parts):
                continue
            for hierarchy in pages.rglob("hierarchy.json"):
                if hierarchy.is_symlink() or any(part.startswith(".") for part in hierarchy.relative_to(source).parts):
                    continue
                labels.append(hierarchy.resolve())
    grouped = {}
    for hierarchy in labels:
        if not hierarchy.is_relative_to(source):
            raise ValueError(f"Label outside input root: {hierarchy}")
        parent = hierarchy.parent.parent
        meta = provenance.get(hierarchy, {})
        item = grouped.setdefault(parent, {"id": meta.get("page_id") or parent.name,
            "input_root": str(source), "page_directory": parent.relative_to(source).as_posix(),
            "url": meta.get("url"), "source_id": meta.get("source_id"),
            "category": meta.get("category"), "category_path": meta.get("category_path"),
            "labels": {}})
        viewport = meta.get("viewport") or hierarchy.parent.name
        item["labels"][viewport] = hierarchy
    for item in grouped.values():
        for viewport in viewports:
            item["labels"].setdefault(viewport, source / item["page_directory"] / viewport / "hierarchy.json")
    return sorted(grouped.values(), key=lambda item: item["page_directory"])
