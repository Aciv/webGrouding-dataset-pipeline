"""Read collected URL/category provenance without guessing missing page origins."""
import json
import os
from pathlib import Path
from urllib.parse import urljoin


def load_json(file):
    """Read existing metadata; malformed JSON must not silently drop URL provenance."""
    return json.loads(Path(file).read_text(encoding="utf-8"))


def index_files(source):
    """Find published indices only, excluding in-flight .work/.publishing trees."""
    root = source.parent if source.name == "pages" else source
    for directory, children, files in os.walk(root, followlinks=False):
        children[:] = sorted(child for child in children
                             if not child.startswith(".") and child not in ("pages", "node_modules"))
        for name in ("collection-index.json", "batch-index.json"):
            if name in files:
                yield Path(directory) / name


def read_provenance(source):
    """Map each published hierarchy path to page URL, entry URL and original class.

    Supports the Curlie export-only root index and existing nested collector batches.
    The return flag identifies a Curlie index whose published pages are authoritative.
    """
    records = {}
    authoritative = False
    for file in index_files(source):
        index = load_json(file)
        root = file.parent
        if index.get("schema") == "curlie_page_target_v1":
            if root == source or (source.name == "pages" and root == source.parent):
                authoritative = True
            for batch in index.get("batches", []):
                candidates = {item["id"]: item for item in batch.get("candidates", [])}
                for page in batch.get("pages", []):
                    candidate = candidates.get(page["source"], {})
                    meta = {"page_id": page["id"], "source_id": page["source"],
                            "url": page.get("url"), "entry_url": candidate.get("url"),
                            "title": page.get("title"), "category": page.get("category"),
                            "category_path": page.get("category_path"), "url_source": "collection-index.json"}
                    for viewport, observation in page.get("viewports", {}).items():
                        records[(root / observation["hierarchy"]).resolve()] = {**meta, "viewport": viewport}
        elif file.name == "batch-index.json":
            sources = {item["id"]: item for item in index.get("config", {}).get("sources", [])}
            entries = {}
            for seed in index.get("config", {}).get("pages", []):
                base = sources.get(seed["source"], {}).get("baseUrl")
                if base:
                    entries.setdefault(seed["source"], urljoin(base, seed["path"]))
            for page in index.get("pages", []):
                if page.get("status") != "captured":
                    continue
                source_config = sources.get(page["source"], {})
                meta = {"page_id": page["id"], "source_id": page["source"],
                        "url": page.get("url"), "entry_url": entries.get(page["source"]),
                        "title": page.get("title"), "category": source_config.get("category"),
                        "category_path": source_config.get("category_path"), "url_source": "batch-index.json"}
                for observation in page.get("observations", []):
                    if observation.get("status") == "captured":
                        records[(root / observation["directory"] / "hierarchy.json").resolve()] = {
                            **meta, "viewport": observation["viewport"]}
    return records, authoritative


def sample_provenance(hierarchy, records):
    """Fall back to a saved DOM URL when the directory has no source index."""
    meta = {"page_id": hierarchy.parent.parent.name, "source_id": None,
            "viewport": hierarchy.parent.name, "url": None, "entry_url": None,
            "title": None, "category": None, "category_path": None, "url_source": "missing"}
    meta.update(records.get(hierarchy.resolve(), {}))
    if not meta["url"]:
        label_url = load_json(hierarchy).get("capture", {}).get("url")
        if label_url:
            meta.update(url=label_url, url_source="hierarchy.json")
    if not meta["url"] and hierarchy.with_name("nodes.json").is_file():
        raw = load_json(hierarchy.with_name("nodes.json"))
        meta.update(url=raw.get("url"), title=meta["title"] or raw.get("title"), url_source="nodes.json")
    if not meta["url"]:
        meta["url_source"] = "missing"
    return meta
