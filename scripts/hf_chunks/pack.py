"""Pack published screenshots and labels into compressed, URL-indexed shards for the Hub.

Standard library only. Reuses the published-page discovery and URL provenance rules from
``export_pages.py`` and ``scripts/webdataset/provenance.py``; it never opens a browser,
re-encodes a screenshot or rewrites the original hierarchy.
"""
import collections
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import struct
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qsl, urlencode, urlsplit

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT / "scripts"
WEB = SCRIPTS / "webdataset"
for _path in (PROJECT, SCRIPTS, WEB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from export_pages import find_pages
from provenance import read_provenance, sample_provenance

PACKAGE_SCHEMA = "responsive_ui_hf_chunks_v1"
SAMPLE_SCHEMA = "responsive_ui_hf_sample_v1"


class HashingReader:
    """Hash PNG bytes while they are streamed into the shard, without a second read."""
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()
        self.bytes = 0

    def read(self, size=-1):
        """Forward one tarfile read and feed exactly those bytes to the digest."""
        data = self.stream.read(size)
        if data:
            self.hash.update(data)
            self.bytes += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()


class HashingWriter:
    """Hash compressed bytes as they are written to the shard file."""
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()
        self.bytes = 0

    def write(self, data):
        """Forward a compressed write and include the same bytes in the digest."""
        self.hash.update(data)
        self.bytes += len(data)
        return self.stream.write(data)

    def flush(self):
        """Flush the underlying file so the live shard size is observable."""
        self.stream.flush()


def training_pair(hierarchy):
    """Resolve full.png/initial.png exactly like export_pages.training_files, once.

    The hierarchy is returned with the pair so callers do not read a roughly 0.5 MB
    label file twice for every viewport.
    """
    hierarchy = Path(hierarchy)
    label = json.loads(hierarchy.read_text(encoding="utf-8"))
    name = label.get("capture", {}).get("image", "initial.png")
    if name not in ("initial.png", "full.png"):
        raise ValueError(f"Unsupported training image name: {name}")
    image = hierarchy.with_name(name)
    if not image.is_file():
        raise FileNotFoundError(f"Missing {name}: {hierarchy.parent}")
    return image, label


def png_dimensions(path):
    """Read width/height from the PNG IHDR without decoding the image."""
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


def normalize_page_url(value):
    """Return the page identity used for cross-run dedupe.

    HTTP/HTTPS are treated as the same page; host casing, a leading ``www.``,
    default ports, fragments, tracking parameters and query order are ignored.
    Path case and non-tracking query parameters remain significant.
    """
    raw = value.strip()
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return raw.lower()
        host = host.removeprefix("www.")
        port = parts.port
        scheme = parts.scheme.lower()
        default_port = ((scheme == "http" and port in (None, 80))
                        or (scheme == "https" and port in (None, 443)))
        netloc = host if default_port else f"{host}:{port}"
        if scheme and scheme not in ("http", "https"):
            netloc = f"{scheme}://{netloc}"
        path = parts.path or "/"
        path = re.sub(r"/index\.html?$", "/", path, flags=re.IGNORECASE)
        query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
                 if not re.match(r"^(?:utm_.*|gclid|fbclid)$", key, flags=re.IGNORECASE)]
        query.sort()
        encoded = urlencode(query, doseq=True)
        return netloc + path + (f"?{encoded}" if encoded else "")
    except (AttributeError, ValueError):
        return raw.lower()


def normalize_site(value):
    """Return a lower-case, www-insensitive host for optional site-level dedupe."""
    raw = value.strip()
    try:
        host = urlsplit(raw if "://" in raw else "https://" + raw).hostname
    except ValueError:
        host = None
    return (host or raw).lower().rstrip(".").removeprefix("www.")


def page_identity(page):
    """Use the first non-null page URL as the group's provenance identity."""
    return next((sample["provenance"]["url"] for sample in page
                 if sample["provenance"]["url"]), None)


def page_reference(page):
    """Describe a page group in the dedupe report without copying image bytes."""
    return {"run_id": page[0]["run_id"], "run_name": page[0]["run_name"],
            "page_id": page[0]["provenance"]["page_id"],
            "source_id": page[0]["provenance"].get("source_id"),
            "source_url": page_identity(page),
            "viewports": sorted(sample["provenance"]["viewport"] for sample in page),
            "image_count": len(page)}


def dedupe_pages(pages, mode):
    """Drop duplicate page groups by normalized URL or host, keeping one whole group.

    Completeness wins first (more viewports), then the earlier input order. Viewports
    from different runs are never merged into one page. Winners are finalized before
    the report is emitted, so every report names the page that was actually kept.
    """
    if mode not in ("url", "site", "none"):
        raise ValueError(f"Unsupported dedupe mode: {mode}")
    if mode == "none":
        return pages, []
    page_keys = []
    kept = {}
    order = []

    def identity(page):
        url = page_identity(page)
        if not url:
            return (None, page[0]["run_id"], page[0]["provenance"]["page_id"])
        return normalize_page_url(url) if mode == "url" else normalize_site(url)

    for page in pages:
        key = identity(page)
        page_keys.append(key)
        if key not in kept:
            kept[key] = page
            order.append(key)
        elif len(page) > len(kept[key]):
            kept[key] = page
    duplicates = []
    for page, key in zip(pages, page_keys):
        winner = kept[key]
        if page is winner:
            continue
        duplicates.append({
            "schema": "responsive_ui_hf_duplicate_v1", "dedupe_by": mode, "key": key,
            "reason": "more_viewports" if len(winner) > len(page) else "first_seen",
            "kept": page_reference(winner), "dropped": page_reference(page)})
    return [kept[key] for key in order], duplicates


def list_samples(source, require_url):
    """Find published image/hierarchy pairs and join their indexed URL provenance.

    Only lightweight metadata is retained; full hierarchy objects for 30k viewports
    would otherwise consume many gigabytes before packaging starts. Labels are read
    again per page while writing.
    """
    records, authoritative = read_provenance(source)
    samples = []
    for pages in find_pages(source):
        if any(part.startswith(".") for part in pages.relative_to(source).parts):
            continue
        for hierarchy in sorted(pages.rglob("hierarchy.json")):
            if hierarchy.is_symlink() or source not in hierarchy.resolve().parents:
                raise ValueError(f"External or symlinked hierarchy: {hierarchy}")
            if authoritative and hierarchy.resolve() not in records:
                continue
            image, label = training_pair(hierarchy)
            if image.is_symlink() or source not in image.resolve().parents:
                raise ValueError(f"External or symlinked image: {image}")
            width, height = png_dimensions(image)
            capture = label.get("capture", {})
            if capture.get("width", width) != width or capture.get("height", height) != height:
                raise ValueError(f"Image/label dimensions differ: {image}")
            meta = sample_provenance(hierarchy, records)
            if require_url and not meta["url"]:
                raise ValueError(f"Missing page URL: {hierarchy}")
            samples.append({"image": image, "hierarchy_path": hierarchy,
                            "provenance": meta, "width": width, "height": height})
    if not samples:
        raise ValueError("No complete published image/hierarchy pairs found")
    return samples


def group_samples(samples):
    """Group every viewport of one published page; a page is the atomic sample.

    A page's resolutions are never split between shards or JSONL rows. Duplicate
    viewports and conflicting page URLs are explicit errors rather than silent merges.
    """
    groups = collections.OrderedDict()
    for sample in samples:
        provenance = sample["provenance"]
        groups.setdefault((sample["run_id"], provenance.get("source_id") or "",
                           provenance["page_id"]), []).append(sample)
    pages = []
    for key in sorted(groups):
        page = sorted(groups[key], key=lambda item: (item["provenance"]["viewport"],
                                                      str(item["hierarchy_path"])))
        viewports = set()
        urls = set()
        for sample in page:
            viewport = sample["provenance"]["viewport"]
            if (viewport in (".", "..")
                    or not re.fullmatch(r"[A-Za-z0-9._-]+", viewport or "")):
                raise ValueError(f"Unsafe viewport id: {viewport!r}")
            if viewport in viewports:
                raise ValueError(f"Duplicate viewport {viewport} for page {key[2]}")
            viewports.add(viewport)
            if sample["provenance"]["url"]:
                urls.add(sample["provenance"]["url"])
        if len(urls) > 1:
            raise ValueError(f"Viewports disagree on page URL: {key[2]}")
        pages.append(page)
    return pages


class ChunkWriter:
    """Write gzip TAR chunks whose measured compressed size stays near the target.

    USTAR framing is written directly with ``TarInfo.tobuf`` instead of tarfile's
    streaming mode, which buffers 10 KiB internally and hides the live shard size.
    """
    def __init__(self, data_directory, target_bytes, prefix="train"):
        self.data_directory = data_directory
        self.target_bytes = target_bytes
        self.prefix = prefix
        self.suffix = ".tar.gz"
        self.shards = []
        self.raw = None
        self.hashing = None
        self.stream = None
        self.destination = None
        self.temporary = None
        self.count = 0

    @property
    def is_open(self):
        """Whether a shard is currently accepting samples."""
        return self.stream is not None

    def _start(self):
        """Open the next shard under a .partial name and compress on the fly."""
        number = len(self.shards)
        self.destination = self.data_directory / f"{self.prefix}-{number:05d}{self.suffix}"
        self.temporary = self.destination.with_name(self.destination.name + ".partial")
        self.raw = self.temporary.open("wb")
        self.hashing = HashingWriter(self.raw)
        self.stream = gzip.GzipFile(fileobj=self.hashing, mode="wb", compresslevel=6, mtime=0)
        self.count = 0

    def current_size(self):
        """Return the compressed or uncompressed shard bytes written so far."""
        if not self.is_open:
            return 0
        self.stream.flush()
        return self.hashing.bytes

    def _write_member(self, name, size, reader):
        """Write one USTAR header, exactly ``size`` payload bytes and zero padding."""
        member = tarfile.TarInfo(name)
        member.size = size
        member.mode = 0o644
        member.mtime = 0
        member.uid = 0
        member.gid = 0
        member.uname = ""
        member.gname = ""
        self.stream.write(member.tobuf(format=tarfile.USTAR_FORMAT))
        remaining = size
        while remaining:
            chunk = reader.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"File ended before its recorded size: {name}")
            self.stream.write(chunk)
            remaining -= len(chunk)
        padding = (-size) % 512
        if padding:
            self.stream.write(b"\0" * padding)

    def add_page(self, index, page_samples):
        """Append all resolutions of one page at their original paths.

        A page is written as a unit, so even a chunk limit smaller than the page cannot
        separate its viewports. Extracting every shard restores the published tree:
        each viewport keeps its original member path; hierarchy bytes may come from
        the pipeline's derived clean version. No synthetic page.json is added.
        """
        if not self.is_open:
            self._start()
        page_id = f"{index:09d}"
        chunk = f"data/{self.destination.name}"
        representative = next((sample for sample in page_samples
                               if sample["provenance"]["url"]), page_samples[0])
        provenance = representative["provenance"]
        record = {
            "schema": SAMPLE_SCHEMA,
            "index": index,
            "sample_id": page_id,
            "page_id": provenance["page_id"],
            "source_id": provenance["source_id"],
            "run_id": page_samples[0]["run_id"],
            "run_name": page_samples[0]["run_name"],
            "source_url": provenance["url"],
            "url": provenance["url"],
            "entry_url": provenance["entry_url"],
            "title": provenance["title"],
            "category": provenance["category"],
            "category_path": provenance["category_path"],
            "url_source": provenance["url_source"],
            "image_count": len(page_samples),
            "chunk": chunk,
            "viewports": {},
        }
        versions = set()
        for sample in page_samples:
            hierarchy_blob = sample["hierarchy_path"].read_bytes()
            hierarchy_sha256 = hashlib.sha256(hierarchy_blob).hexdigest()
            expected_hash = sample.get("hierarchy_sha256")
            if expected_hash and hierarchy_sha256 != expected_hash:
                raise ValueError(f"Hierarchy changed or mismatched: {sample['hierarchy_path']}")
            label = json.loads(hierarchy_blob.decode("utf-8"))
            rule_version = label.get("rule_version", "unversioned")
            versions.add(rule_version)
            viewport = sample["provenance"]["viewport"]
            prefix = sample.get("archive_prefix", "")
            image_rel = sample.get("image_rel") or sample["image"].relative_to(sample["source"]).as_posix()
            hierarchy_rel = (sample.get("hierarchy_rel")
                             or sample["hierarchy_path"].relative_to(sample["source"]).as_posix())
            image_member = prefix + image_rel
            hierarchy_member = prefix + hierarchy_rel
            size = sample["image"].stat().st_size
            with HashingReader(sample["image"].open("rb")) as stream:
                self._write_member(image_member, size, stream)
                if stream.bytes != size:
                    raise ValueError(f"Image changed while packaging: {sample['image']}")
                image_sha256 = stream.hash.hexdigest()
            self._write_member(hierarchy_member, len(hierarchy_blob), io.BytesIO(hierarchy_blob))
            capture = label.get("capture", {})
            entry = {
                "image_member": image_member,
                "hierarchy_member": hierarchy_member,
                "image_bytes": size,
                "image_sha256": image_sha256,
                "hierarchy_bytes": len(hierarchy_blob),
                "hierarchy_sha256": hierarchy_sha256,
                "width": sample["width"],
                "height": sample["height"],
                "captured_at": capture.get("capturedAt"),
                "annotation_rule_version": rule_version,
                "hierarchy_source": sample.get("hierarchy_side", "original"),
                "original_image_name": sample["image"].name,
                "original_image_path": image_rel,
                "original_hierarchy_path": hierarchy_rel,
            }
            if label.get('detail_training_selection'):
                entry['detail_trainable_crops'] = len(label['detail_training_crop_ids'])
                entry['discarded_detail_crops'] = len(label['discarded_detail_crop_ids'])
            if sample.get("hierarchy_side") == "derived":
                entry["derived_hierarchy_path"] = sample.get("derived_hierarchy_path")
            record["viewports"][viewport] = entry
        record["annotation_rule_version"] = next(iter(versions)) if len(versions) == 1 else "mixed"
        self.count += 1
        return record

    def close(self):
        """Finish the TAR end blocks, compression, and atomic rename without .partial."""
        if not self.is_open:
            return
        self.stream.write(b"\0" * 1024)
        if not self.stream.closed:
            self.stream.close()
        self.hashing.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()
        self.temporary.replace(self.destination)
        self.shards.append({"file": f"data/{self.destination.name}", "samples": self.count,
                            "bytes": self.destination.stat().st_size,
                            "sha256": self.hashing.hash.hexdigest()})
        self.raw = None
        self.hashing = None
        self.stream = None
        self.destination = None
        self.temporary = None

    def abort(self):
        """Close interrupted streams but leave the .partial file out of the manifest."""
        if self.raw is not None:
            try:
                self.raw.close()
            except OSError:
                pass
        self.stream = None
        self.raw = None
        self.hashing = None


def dataset_card(summary, curlie):
    """Write a data card that explains the page-grouped compressed shard layout."""
    if summary.get('label_contract') == 'gui_component_pack_input_v1':
        return ('# Processed UI component dataset\n\n'
                f"{summary['samples']} page groups, {summary['images']} full screenshots.\n\n"
                '`metadata.jsonl` records page URLs and per-viewport image/label members in '
                '`data/*.tar.gz`. Only accepted viewports are present; a page can have fewer '
                'than three viewports. Each pair is `pages/<page>/<viewport>/full.png` and '
                '`hierarchy.json` with schema `gui_processed_page_v1`. PNG bytes are unchanged.\n\n'
                'Labels contain a compact component graph, usable crops and eligible split '
                'supervision. Boxes use xyxy: nodes in whole-image coordinates; crop targets '
                'and ignore regions in crop-local coordinates. Extract images with `crop_bbox`; '
                'respect ignore regions and `split_supervision_eligible`. If present, consume only '
                '`crops` with `detail_training_eligible=true`; coverage/pixel-discarded crops remain marked '
                'for audit, without discarding sibling crops or full-page cut supervision. These are rule-generated '
                'unverified labels. Final nodes retain true parent depth L1-L5 (layer 0..4), '
                'without promoting deeper nodes. Existing fusion and cut coordinates are preserved. Keep page/site groups together '
                'when splitting train/validation/test. Website screenshot rights remain separate '
                'from metadata attribution.\n')
    derived = summary.get("hierarchy_policy") == "derived_clean_hierarchy"
    hierarchy_note = (
        "Hierarchy members in this package are the pipeline's postprocessed clean "
        "L1-L5 labels; image members remain the original screenshot bytes. Per-viewport "
        "`hierarchy_source`, `derived_hierarchy_path`, rule version and SHA-256 record "
        "the derived origin."
        if derived else
        "Hierarchy members in this package are the original published hierarchy bytes.")
    lines = [
        "---", "pretty_name: Responsive UI Structure (chunked)",
        "task_categories:", "  - object-detection",
        "tags:", "  - ui", "  - responsive", "  - webpage-screenshots",
        "---", "",
        "# Responsive UI Structure (chunked)", "",
        f"{summary['samples']} page samples ({summary['images']} screenshots) in "
        f"{len(summary['shards'])} gzip-compressed TAR "
        f"{'archive' if len(summary['shards']) == 1 else 'archives'} "
        f"({summary['chunk_size_mb']} MiB target per chunk).",
        "One page sample contains every captured viewport of that page. All three resolutions "
        "are stored in the same JSONL row and the same chunk; chunk boundaries are only placed "
        "between pages. Their files keep the original capture directories and filenames.", "",
        "Every shard member uses the published file's original relative path, for example "
        "`<page>/<viewport>/full.png` and `<page>/<viewport>/hierarchy.json`. Extracting all "
        "shards therefore restores the published tree instead of a renamed or flattened copy. "
        "No synthetic page JSON is inserted into that tree.", "",
        "`metadata.jsonl` is the index for every page sample. It contains the zero-based `index`, "
        "the captured page `source_url` (also stored as `url`), `entry_url`, category, "
        "`annotation_rule_version`, `run_id`, `run_name`, `chunk`, and a `viewports` map with "
        "each viewport's `image_member`, `hierarchy_member`, dimensions, capture time, source "
        "path and SHA-256 digests. Source URLs are read from collection/batch indices or the "
        "saved hierarchy; they are never inferred from directory names. Missing provenance is null.", "",
        f"Pages were merged from {len(summary['inputs'])} input "
        f"{'run' if len(summary['inputs']) == 1 else 'runs'}. The manifest maps each "
        "`run_id` to its run name and kept/dropped page counts. "
        + (f"Deduplication mode `{summary['dedupe']['by']}` kept "
           f"{summary['dedupe']['kept_pages']} of {summary['input_pages']} page groups; "
           "`duplicates.jsonl` records every dropped page, the kept page, and the reason."
           if summary['dedupe']['by'] != "none" else
           "Deduplication was disabled; duplicate page URLs may remain."), "",
        ("A single input run extracts back to its exact published tree. Multiple runs keep "
         "each tree under `run-<id>/`; `manifest.json` maps every id to its run name."
         if len(summary['inputs']) > 1 else
         "Extracting the shards restores the published tree exactly for this single input run."), "",
        "The shards are ordinary gzip-compressed TAR archives preserving the original tree. "
        "They are not declared as Hub WebDataset `data_files`: WebDataset pairs files by key, "
        "while this packer groups a page's viewports into one JSON index row. Read "
        "`metadata.jsonl`, or extract the shards and use the original hierarchy files.", "",
    ]
    workers = summary.get("pack_workers", 1)
    if workers > 1:
        lines += [f"Shards were written by {workers} parallel workers; the worker id is part of "
                  "each file name (`train-NN-xxxxx.tar.gz`). Pages remain atomic inside one shard, "
                  "and `metadata.jsonl` still points to the exact member paths.", ""]
    lines += [
        "```python",
        "import json, tarfile",
        "",
        "rows = [json.loads(line) for line in open('metadata.jsonl', encoding='utf-8')]",
        "row = rows[0]",
        "with tarfile.open(row['chunk'], 'r:*') as archive:",
        "    for viewport, entry in row['viewports'].items():",
        "        png = archive.extractfile(entry['image_member']).read()",
        "        hierarchy = json.load(archive.extractfile(entry['hierarchy_member']))",
        "        print(row['index'], viewport, row['source_url'], hierarchy['capture'])",
        "```", "",
        "`viewports[<viewport>].image_member` and `hierarchy_member` locate the "
        "`full.png`/`initial.png` and `hierarchy.json` inside `chunk`; their member paths match "
        "the source collection. `original_image_path` and `original_hierarchy_path` repeat those "
        "paths for traceability. The JSONL row omits the bulky hierarchy objects themselves.", "",
        hierarchy_note, "",
        "Annotations are rule-generated candidates, not human-verified ground truth. "
        "No train/validation/test partition is implied: `train` is only the shard prefix. "
        "Keep all resolutions and pages of a site in the same split when creating evaluation data.", "",
        "Website screenshot rights are separate from directory metadata attribution.",
    ]
    if curlie:
        lines += ["", "Where Curlie metadata is included: With content from Curlie.org - the largest "
                  "human-edited directory of the web. Contribute by submitting a website or becoming "
                  "an editor. https://curlie.org/docs/en/license.html"]
    lines.append("")
    return "\n".join(lines)


def resolve_inputs(inputs):
    """Accept one path or a list and reject duplicates, nesting and missing roots."""
    supplied = [inputs] if isinstance(inputs, (str, Path)) else list(inputs)
    if not supplied:
        raise ValueError("At least one input directory is required")
    roots = []
    seen = set()
    for value in supplied:
        root = Path(value).resolve()
        if not root.is_dir():
            raise ValueError(f"Input does not exist: {root}")
        if root in seen:
            raise ValueError(f"Duplicate input directory: {root}")
        seen.add(root)
        roots.append(root)
    for index, first in enumerate(roots):
        for second in roots[index + 1:]:
            if first in second.parents or second in first.parents:
                raise ValueError("Input directories must not contain each other")
    return roots


def _validate_package_options(output, chunk_size_mb, dedupe_by, roots=(), derived_root=None,
                              workers=1):
    """Validate output ownership and chunk options before any member is written."""
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    output = Path(output).resolve()
    for root in roots:
        if output == root or root in output.parents or output in root.parents:
            raise ValueError("Input and output directories must not contain each other")
    if derived_root is not None:
        derived_root = Path(derived_root).resolve()
        if output == derived_root or derived_root in output.parents or output in derived_root.parents:
            raise ValueError("Packaging output and derived hierarchies must not contain each other")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output must be empty; completed chunks are never overwritten")
    if chunk_size_mb <= 0:
        raise ValueError("Chunk size must be positive")
    if dedupe_by not in ("url", "site", "none"):
        raise ValueError(f"Unsupported dedupe mode: {dedupe_by}")
    return output


def _curlie_source(roots):
    """Detect a Curlie authority index for the data card attribution."""
    for root in roots:
        index_file = root / "collection-index.json"
        if index_file.is_file():
            try:
                if (json.loads(index_file.read_text(encoding="utf-8")).get("schema")
                        == "curlie_page_target_v1"):
                    return True
            except (OSError, json.JSONDecodeError):
                pass
    return False


def _write_pages_parallel(pages, data_directory, target_bytes, workers, progress, started):
    """Write contiguous page buckets with independent gzip streams.

    Each worker gets a unique shard prefix so page-atomic chunks can be compressed in
    parallel; rows are returned with globally unique indexes and merged afterwards.
    """
    total = len(pages)
    workers = min(workers, total)
    size, extra = divmod(total, workers)
    buckets, offset = [], 0
    for number in range(workers):
        count = size + (1 if number < extra else 0)
        buckets.append((number, offset, pages[offset:offset + count]))
        offset += count
    interval = max(1, total // 20)
    lock = threading.Lock()
    state = {"written": 0, "shards_closed": 0}

    def work(number, start, bucket):
        writer = ChunkWriter(data_directory, target_bytes, prefix=f"train-{number:02d}")
        rows = []
        try:
            for local, page in enumerate(bucket):
                rows.append(writer.add_page(start + local, page))
                if writer.current_size() >= target_bytes:
                    writer.close()
                    with lock:
                        state["shards_closed"] += 1
                with lock:
                    state["written"] += 1
                    written = state["written"]
                    if progress and (written == total
                                     or (total > 20 and written % interval == 0)):
                        print(json.dumps({"type": "pack_progress", "phase": "running",
                                          "written": written, "total": total,
                                          "percent": round(written * 100 / total, 1),
                                          "shards_closed": state["shards_closed"],
                                          "workers": workers,
                                          "seconds": round(time.monotonic() - started, 1)},
                                         ensure_ascii=False), flush=True)
            was_open = writer.is_open
            writer.close()
            if was_open:
                with lock:
                    state["shards_closed"] += 1
            return rows, writer.shards
        except BaseException:
            writer.abort()
            raise

    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, number, start, bucket)
                   for number, start, bucket in buckets]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except BaseException as error:
                errors.append(error)
    if errors:
        raise errors[0]
    rows = sorted((row for page_rows, _ in results for row in page_rows),
                  key=lambda row: row["index"])
    shards = sorted((shard for _, page_shards in results for shard in page_shards),
                    key=lambda shard: shard["file"])
    return rows, shards


def _write_dataset(samples, input_stats, output, chunk_size_mb, seed, dedupe_by, roots,
                   extra_summary=None, progress=False, workers=1):
    """Group samples into atomic pages, dedupe, and write shards and metadata."""
    pages = group_samples(samples)
    stats_by_run = {item["id"]: item for item in input_stats}
    for page in pages:
        stats_by_run[page[0]["run_id"]]["pages"] += 1
    pages, duplicates = dedupe_pages(pages, dedupe_by)
    for page in pages:
        stats = stats_by_run[page[0]["run_id"]]
        stats["kept_pages"] += 1
        stats["kept_images"] += len(page)
    random.Random(seed).shuffle(pages)
    kept_samples = [sample for page in pages for sample in page]
    target_bytes = int(chunk_size_mb * 1024 * 1024)
    total_pages = len(pages)
    interval = max(1, total_pages // 20)
    started = time.monotonic()
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    workers = min(workers, max(1, total_pages))
    if progress:
        print(json.dumps({"type": "pack_progress", "phase": "start", "pages": total_pages,
                          "images": len(kept_samples), "dropped_pages": len(duplicates),
                          "workers": workers}, ensure_ascii=False), flush=True)
    (output / "data").mkdir(parents=True, exist_ok=True)
    categories = collections.Counter()
    viewports = collections.Counter()
    versions = collections.Counter()
    viewports_per_page = collections.Counter()
    missing_url = 0
    if workers > 1:
        rows, writer_shards = _write_pages_parallel(
            pages, output / "data", target_bytes, workers, progress, started)
        for row in rows:
            categories[row["category"] or "Unknown"] += 1
            viewports.update(row["viewports"].keys())
            versions[row["annotation_rule_version"]] += 1
            viewports_per_page[row["image_count"]] += 1
            missing_url += not bool(row["source_url"])
        index_stream = (output / "metadata.jsonl.partial").open("w", encoding="utf-8")
        try:
            for row in rows:
                index_stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            index_stream.flush()
            os.fsync(index_stream.fileno())
        finally:
            index_stream.close()
        (output / "metadata.jsonl.partial").replace(output / "metadata.jsonl")
    else:
        writer = ChunkWriter(output / "data", target_bytes)
        index_stream = (output / "metadata.jsonl.partial").open("w", encoding="utf-8")
        try:
            for number, page in enumerate(pages):
                row = writer.add_page(number, page)
                index_stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                categories[row["category"] or "Unknown"] += 1
                viewports.update(row["viewports"].keys())
                versions[row["annotation_rule_version"]] += 1
                viewports_per_page[row["image_count"]] += 1
                missing_url += not bool(row["source_url"])
                if writer.current_size() >= target_bytes:
                    writer.close()
                    index_stream.flush()
                written = number + 1
                if progress and (written == total_pages
                                 or (total_pages > 20 and written % interval == 0)):
                    print(json.dumps({"type": "pack_progress", "phase": "running",
                                      "written": written, "total": total_pages,
                                      "percent": round(written * 100 / total_pages, 1),
                                      "shards": len(writer.shards) + (1 if writer.is_open else 0),
                                      "seconds": round(time.monotonic() - started, 1)},
                                     ensure_ascii=False), flush=True)
            writer.close()
            index_stream.flush()
            os.fsync(index_stream.fileno())
        except BaseException:
            writer.abort()
            index_stream.close()
            raise
        index_stream.close()
        (output / "metadata.jsonl.partial").replace(output / "metadata.jsonl")
        writer_shards = writer.shards

    if dedupe_by != "none":
        duplicate_stream = (output / "duplicates.jsonl.partial").open("w", encoding="utf-8")
        for record in duplicates:
            duplicate_stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        duplicate_stream.flush()
        os.fsync(duplicate_stream.fileno())
        duplicate_stream.close()
        (output / "duplicates.jsonl.partial").replace(output / "duplicates.jsonl")

    source_ids = {sample["provenance"]["source_id"] for sample in kept_samples
                  if sample["provenance"]["source_id"]}
    prefix_runs = len(input_stats) > 1
    summary = {
        "schema": PACKAGE_SCHEMA,
        "status": "completed",
        "inputs": input_stats,
        "input_pages": sum(item["pages"] for item in input_stats),
        "input_images": len(samples),
        "samples": len(pages),
        "pages": len(pages),
        "images": len(kept_samples),
        "sources": len(source_ids),
        "sample_unit": "one page with all captured viewports",
        "dedupe": {"by": dedupe_by, "kept_pages": len(pages),
                   "dropped_pages": len(duplicates)},
        "compression": "gzip",
        "chunk_size_mb": chunk_size_mb,
        "seed": seed,
        "missing_url": missing_url,
        "categories": dict(categories),
        "viewports": dict(viewports),
        "viewports_per_page": {str(count): pages for count, pages in viewports_per_page.items()},
        "annotation_versions": dict(versions),
        "shards": writer_shards,
        "pack_workers": workers,
        "archive_layout": "original_published_tree",
        "archive_prefix": "run-<id>/" if prefix_runs else "",
        "layout": "<original relative path>/<viewport>/<full.png|initial.png|hierarchy.json>",
        "image_policy": "original_png_bytes",
        "hierarchy_policy": "original_hierarchy_bytes",
        "index_policy": "metadata.jsonl one row per page; members keep original relative paths",
    }
    if extra_summary:
        summary.update(extra_summary)
    curlie = _curlie_source(roots)
    (output / "README.md").write_text(dataset_card(summary, curlie), encoding="utf-8")
    manifest = output / "manifest.json.partial"
    manifest.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest.replace(output / "manifest.json")
    return summary


def pack_dataset(inputs, output, chunk_size_mb=1024, seed=42, require_url=False,
                 dedupe_by="url", progress=False, workers=1):
    """Merge collection runs, dedupe whole page groups, and write gzip shards.

    ``inputs`` may be one path or a list of collection roots. Dedupe never separates
    a page's viewports and never merges viewports from different runs.
    """
    roots = resolve_inputs(inputs)
    output = _validate_package_options(output, chunk_size_mb, dedupe_by, roots,
                                       workers=workers)
    samples = []
    input_stats = []
    prefix_runs = len(roots) > 1
    for number, root in enumerate(roots):
        run_id = f"run-{number:03d}"
        run_samples = list_samples(root, require_url)
        for sample in run_samples:
            sample["source"] = root
            sample["run_id"] = run_id
            sample["run_name"] = root.name
            # A single run extracts back to its exact root; multiple runs need a
            # top-level run folder to avoid collapsing identical relative paths.
            sample["archive_prefix"] = f"{run_id}/" if prefix_runs else ""
        samples.extend(run_samples)
        input_stats.append({"id": run_id, "name": root.name, "images": len(run_samples),
                            "pages": 0, "kept_pages": 0, "kept_images": 0})
    return _write_dataset(samples, input_stats, output, chunk_size_mb, seed, dedupe_by, roots,
                          progress=progress, workers=workers)


def pack_index(index_path, output, chunk_size_mb=1024, seed=42, require_url=False,
               dedupe_by="url", resume=False, progress=False, workers=1):
    """Package pages selected by a pipeline index.

    Screenshots resolve from each page's original run root. Hierarchies resolve from
    the original run when ``hierarchy_side`` is ``original`` and from the index's
    ``hierarchies_root`` after postprocessing. Archive member paths stay the original
    published paths, so only the hierarchy bytes differ from a directory-scan package.
    """
    from pipeline.index import load_index, resolve_path, sha256_file

    index_path = Path(index_path).resolve()
    index = load_index(index_path)
    component_input = index.get('label_contract') == 'gui_component_pack_input_v1'
    if component_input and index.get('final_layer_projection',{}).get('max_layers') != 5:
        raise ValueError('Component packages require final L1-L5 labels; rerun prepare-pack with the current pipeline')
    index_hash = sha256_file(index_path)
    derived_root = (Path(index["hierarchies_root"]).resolve()
                    if index.get("hierarchies_root") else None)
    if derived_root is not None and not derived_root.is_dir():
        raise ValueError(f"Derived hierarchy root does not exist: {derived_root}")
    run_roots = [(run["id"], ((index_path.parent / run["root"]).resolve()
                  if component_input else Path(run["root"]).resolve())) for run in index["runs"]]
    roots = [root for _, root in run_roots]
    output_path = Path(output).resolve()
    if output_path.exists() and any(output_path.iterdir()):
        manifest_path = output_path / "manifest.json"
        existing = None
        if resume and manifest_path.is_file():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
        if existing:
            own = existing.get("pipeline_index") or {}
            same = (existing.get("status") == "completed"
                    and own.get("sha256") == index_hash
                    and existing.get("chunk_size_mb") == chunk_size_mb
                    and existing.get("seed") == seed
                    and (existing.get("dedupe") or {}).get("by") == dedupe_by
                    and existing.get("pack_workers", 1) == workers
                    and (not require_url or existing.get("missing_url", 0) == 0))
            if same:
                existing["resumed"] = True
                if progress:
                    print(json.dumps({"type": "pack_progress", "phase": "skipped",
                                      "pages": existing.get("pages"),
                                      "output": str(output_path)}, ensure_ascii=False), flush=True)
                return existing
            raise ValueError("Existing packages belong to a different index or options; "
                             "choose a new output directory")
        raise ValueError("Output must be empty; completed chunks are never overwritten")
    output = _validate_package_options(output_path, chunk_size_mb, dedupe_by, roots, derived_root,
                                       workers=workers)
    run_lookup = dict(run_roots)
    required = list(index["required_viewports"])
    prefix_runs = len(run_roots) > 1
    input_stats = [{"id": run_id, "name": root.name, "images": 0,
                    "pages": 0, "kept_pages": 0, "kept_images": 0}
                   for run_id, root in run_roots]
    stats_by_run = {item["id"]: item for item in input_stats}
    samples = []
    skipped_pages = 0
    for page in index["pages"]:
        if page.get("status") != "valid":
            skipped_pages += 1
            continue
        run_id = page["run_id"]
        if run_id not in run_lookup:
            raise ValueError(f"Page references unknown run: {run_id}")
        root = run_lookup[run_id]
        # Component filtering is per observation: keep the accepted siblings only.
        for viewport in (sorted(page['viewports']) if component_input else required):
            record = page["viewports"].get(viewport)
            if record is None:
                raise ValueError(f"Valid page {page.get('page_id')} lacks viewport {viewport}")
            if not record.get("image"):
                raise ValueError(f"Valid page {page.get('page_id')} lacks an image for {viewport}")
            side = record.get("hierarchy_side", "original")
            base = derived_root if side == "derived" else root
            if base is None:
                raise ValueError(f"Page {page.get('page_id')} needs a derived hierarchy root")
            image = resolve_path(root, record["image"], f"{page.get('page_id')}.{viewport}.image")
            hierarchy = resolve_path(base, record["hierarchy"], f"{page.get('page_id')}.{viewport}.hierarchy")
            label = json.loads(hierarchy.read_text(encoding="utf-8"))
            if not isinstance(label, dict):
                raise ValueError(f"Hierarchy must be a JSON object: {hierarchy}")
            if component_input:
                from scripts.offline_postprocess.domain import graph_depths
                depths = graph_depths(label['nodes'])
                if any(depth>=5 for depth in depths.values()) or any(
                        n.get('layer')!=depths[n['node_id']] or n.get('derived_depth')!=depths[n['node_id']]
                        for n in label['nodes']):
                    raise ValueError(f'Component package contains nodes beyond true L5: {hierarchy}')
                identifiers = {n['global_node_id'] for n in label['nodes']}
                if any(t['global_node_id'] not in identifiers or t.get('layer',5)>=5
                       for crop in label['crops'] for t in crop['targets']):
                    raise ValueError(f'Deep or dangling crop positive in component package: {hierarchy}')
            capture = label.get("capture", {})
            name = capture.get("image", "initial.png") if isinstance(capture, dict) else None
            if name not in ("full.png", "initial.png") or Path(record["image"]).name != name:
                raise ValueError(f"Image name disagrees with capture.image: {hierarchy}")
            width, height = png_dimensions(image)
            if (capture.get("width", width) != width or capture.get("height", height) != height):
                raise ValueError(f"Image/label dimensions differ: {image}")
            url = page.get("url")
            if require_url and not url:
                raise ValueError(f"Missing page URL: {page.get('page_id')}")
            provenance = {
                "page_id": page.get("page_id"), "source_id": page.get("source_id"),
                "viewport": viewport, "url": url, "entry_url": page.get("entry_url"),
                "title": page.get("title"), "category": page.get("category"),
                "category_path": page.get("category_path"),
                "url_source": page.get("url_source") or "pipeline_index",
            }
            sample = {
                "image": image, "hierarchy_path": hierarchy, "provenance": provenance,
                "width": width, "height": height, "source": root, "run_id": run_id,
                "run_name": root.name,
                "archive_prefix": f"{run_id}/" if prefix_runs else "",
                "image_rel": record["image"],
                "hierarchy_rel": record.get("original_hierarchy") or record["hierarchy"],
                "hierarchy_side": side,
                "hierarchy_sha256": record.get("hierarchy_sha256"),
            }
            if side == "derived":
                sample["derived_hierarchy_path"] = record["hierarchy"]
            samples.append(sample)
            stats_by_run[run_id]["images"] += 1
    if not samples:
        raise ValueError("No valid pages in the pipeline index")
    any_derived = any(sample["hierarchy_side"] == "derived" for sample in samples)
    extra = {
        'label_contract': index.get('label_contract'),
        'detail_training_selection': index.get('detail_training_selection'),
        'final_layer_projection': index.get('final_layer_projection'),
        'detail_training_counts': index.get('detail_training_counts'),
        "pipeline_index": {
            "path": str(index_path),
            "sha256": index_hash,
            "stage": index["stage"],
            "hierarchies_root": str(derived_root) if derived_root else None,
            "required_viewports": required,
            "skipped_pages": skipped_pages,
        },
        "hierarchy_policy": ('prepared_component_labels' if component_input else
                             "derived_clean_hierarchy" if any_derived else "original_hierarchy_bytes"),
    }
    return _write_dataset(samples, input_stats, output, chunk_size_mb, seed,
                          dedupe_by, roots, extra_summary=extra, progress=progress,
                          workers=workers)
