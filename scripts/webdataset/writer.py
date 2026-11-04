"""Standard-library WebDataset writer; no image recompression or label rewriting."""
import collections
import io
import json
import math
from pathlib import Path
import random
import struct
import tarfile
from export_pages import find_pages, training_files
from provenance import read_provenance, sample_provenance


def list_samples(source, require_url):
    """Reuse exported-pair discovery, excluding temporary and unpublished page trees."""
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
            image, _ = training_files(hierarchy)
            if image.is_symlink() or source not in image.resolve().parents:
                raise ValueError(f"External or symlinked image: {image}")
            meta = sample_provenance(hierarchy, records)
            if require_url and not meta["url"]:
                raise ValueError(f"Missing page URL: {hierarchy}")
            samples.append({"image": image, "hierarchy": hierarchy, "metadata": meta})
    if not samples:
        raise ValueError("No complete published image/hierarchy pairs found")
    return samples


class ShardWriter:
    """Write adjacent same-key PNG/JSON pairs, atomically completing each TAR shard."""
    def __init__(self, root, byte_limit, sample_limit):
        self.root = root
        self.byte_limit = byte_limit
        self.sample_limit = sample_limit
        self.shards = []
        self.tar = None
        self.count = 0
        self.payload = 0

    def close(self):
        """Finish TAR framing before exposing the shard without its .partial suffix."""
        if self.tar is None:
            return
        self.tar.close()
        self.temporary.replace(self.destination)
        self.shards.append({"file": f"data/{self.destination.name}", "samples": self.count,
                            "bytes": self.destination.stat().st_size})
        self.tar = None

    def add(self, key, image, metadata):
        """Copy the PNG bytes unchanged; shard limits never split a sample's pair."""
        blob = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
        needed = 1024 + math.ceil(image.stat().st_size / 512) * 512 + math.ceil(len(blob) / 512) * 512
        if self.tar and (self.count >= self.sample_limit or self.payload + needed + 10240 > self.byte_limit):
            self.close()
        if self.tar is None:
            self.destination = self.root / f"train-{len(self.shards):05d}.tar"
            self.temporary = self.destination.with_suffix(".tar.partial")
            self.tar = tarfile.open(self.temporary, "w", format=tarfile.USTAR_FORMAT)
            self.count = 0
            self.payload = 0
        for suffix, size, stream in [
            ("png", image.stat().st_size, image.open("rb")),
            ("json", len(blob), io.BytesIO(blob)),
        ]:
            with stream:
                member = tarfile.TarInfo(f"{key}.{suffix}")
                member.size = size
                member.mode = 0o644
                self.tar.addfile(member, stream)
        self.count += 1
        self.payload += needed

    def abort(self):
        """Close interrupted output without publishing a partially written shard."""
        if self.tar:
            self.tar.close()
            self.tar = None


def dataset_card(samples, shards):
    """Declare only TAR data files so auxiliary JSON indices are not mistaken for data."""
    return f'''---
pretty_name: Responsive UI Structure
task_categories:
  - object-detection
tags:
  - webdataset
  - ui
  - responsive
configs:
  - config_name: default
    data_files:
      - split: train
        path: data/train-*.tar
---

# Responsive UI Structure

{samples} screenshot samples in {shards} WebDataset TAR shards.
Each sample has an unchanged `.png` screenshot and a same-key `.json` metadata file.
JSON includes the page `url`, `entry_url`, `category`, `category_path`, `viewport`,
capture dimensions/time and original `hierarchy`. Missing provenance is null, not inferred.
`image_file` locates the PNG inside its shard; `original_image_name` and
`hierarchy.capture.image` retain the source capture filename.

Annotations are rule-generated candidates, not human-verified ground truth.
No train/validation/test partition is inferred: `train` is the packaging split only.
Keep all resolutions/pages of a site in the same split when creating evaluation data.

```python
from datasets import load_dataset
data = load_dataset("YOUR_USERNAME/YOUR_DATASET", split="train", streaming=True)
sample = next(iter(data))
image = sample["png"]
metadata = sample["json"]
url = metadata["url"]
hierarchy = metadata["hierarchy"]
```

Hugging Face can recognize the standard WebDataset layout. Viewer availability
depends on Hub conversion, storage and account settings; no remote upload was performed.
Custom layer/parent overlay rendering is not provided by this package.

Website screenshot rights are separate from directory metadata attribution.
Where Curlie metadata is included: With content from Curlie.org - the largest
human-edited directory of the web. Contribute by submitting a website or becoming an editor.
https://curlie.org/docs/en/license.html
'''


def pack_dataset(source, output, shard_size_mb=512, samples_per_shard=1000, seed=42, require_url=False):
    """Package collected pairs plus indexed URL metadata without altering the source."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError(f"Input does not exist: {source}")
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("Input and output directories must not contain each other")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output must be empty; completed shards are never overwritten")
    if shard_size_mb <= 0 or samples_per_shard < 1:
        raise ValueError("Shard size and sample count must be positive")
    samples = list_samples(source, require_url)
    random.Random(seed).shuffle(samples)
    (output / "data").mkdir(parents=True, exist_ok=True)
    writer = ShardWriter(output / "data", int(shard_size_mb * 1024 * 1024), samples_per_shard)
    categories = collections.Counter()
    viewports = collections.Counter()
    missing_url = 0
    versions = collections.Counter()
    try:
        with (output / "samples.jsonl.partial").open("w") as index:
            for number, sample in enumerate(samples):
                key = f"sample-{number:09d}"
                hierarchy = json.loads(sample["hierarchy"].read_text())
                with sample["image"].open("rb") as stream:
                    header = stream.read(24)
                if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
                    raise ValueError(f"Not a PNG: {sample['image']}")
                width, height = struct.unpack(">II", header[16:24])
                capture = hierarchy.get("capture", {})
                if capture.get("width", width) != width or capture.get("height", height) != height:
                    raise ValueError(f"Image/label dimensions differ: {sample['image']}")
                metadata = {"schema": "responsive_ui_webdataset_v1", "sample_id": key,
                            **sample["metadata"], "width": width, "height": height,
                            "captured_at": capture.get("capturedAt"), "image_file": f"{key}.png",
                            "original_image_name": sample["image"].name,
                            "original_path": sample["image"].relative_to(source).as_posix(),
                            "annotation_rule_version": hierarchy.get("rule_version", "unversioned"),
                            "hierarchy": hierarchy}
                writer.add(key, sample["image"], metadata)
                row = {k: value for k, value in metadata.items() if k != "hierarchy"}
                row.update(shard=f"data/{writer.destination.name}", metadata_file=f"{key}.json")
                index.write(json.dumps(row, ensure_ascii=False) + "\n")
                categories[metadata["category"] or "Unknown"] += 1
                viewports[metadata["viewport"]] += 1
                missing_url += not bool(metadata["url"])
                versions[metadata["annotation_rule_version"]] += 1
        writer.close()
        (output / "samples.jsonl.partial").replace(output / "samples.jsonl")
        summary = {"schema": "responsive_ui_webdataset_package_v1", "status": "completed",
                   "samples": len(samples), "missing_url": missing_url,
                   "categories": dict(categories), "viewports": dict(viewports), "seed": seed,
                   "annotation_versions": dict(versions),
                   "shard_size_mb": shard_size_mb, "samples_per_shard": samples_per_shard,
                   "shards": writer.shards, "image_policy": "original_png_bytes",
                   "hierarchy_policy": "original_object_in_metadata"}
        (output / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        (output / "README.md").write_text(dataset_card(len(samples), len(writer.shards)))
        return summary
    except BaseException:
        writer.abort()
        raise
