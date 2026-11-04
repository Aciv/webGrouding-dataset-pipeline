"""Small local checks for paired TAR layout, provenance and unmodified annotations."""
import base64
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from writer import pack_dataset

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=")


def fixture(root, viewport="desktop"):
    """Create a tiny canonical pair, with extra diagnostic files that must not be packed."""
    folder = root / "pages/example" / viewport
    folder.mkdir(parents=True, exist_ok=True)
    hierarchy = {"capture": {"image": "full.png", "width": 1, "height": 1,
                             "capturedAt": "2026-11-02T00:00:00Z"}, "nodes": [],
                 "annotation_status": "rule_generated_unverified"}
    (folder / "full.png").write_bytes(PNG)
    (folder / "hierarchy.json").write_text(json.dumps(hierarchy))
    (folder / "overlay.png").write_bytes(b"do not pack")
    return folder, hierarchy


class PackagingTests(unittest.TestCase):
    """No browser, network or non-standard dependency is required for these tests."""
    def test_curlie_provenance_shards_and_excluded_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input"
            _, hierarchy = fixture(source)
            fixture(source, "mobile")
            fixture(source / ".work/00001")
            candidate = {"id": "site", "url": "https://example.com/entry?a=1"}
            page = {"id": "example", "source": "site", "url": "https://example.com/article?x=2",
                    "title": "Article", "category": "News", "category_path": "News/Media",
                    "viewports": {v: {"hierarchy": f"pages/example/{v}/hierarchy.json"} for v in ("desktop", "mobile")}}
            (source / "collection-index.json").write_text(json.dumps({"schema": "curlie_page_target_v1",
                "batches": [{"candidates": [candidate], "pages": [page]}]}))
            output = root / "output"
            result = pack_dataset(source, output, samples_per_shard=1, require_url=True)
            self.assertEqual(result["samples"], 2)
            self.assertEqual(len(result["shards"]), 2)
            for shard in result["shards"]:
                with tarfile.open(output / shard["file"]) as archive:
                    members = archive.getnames()
                    self.assertEqual(len(members), 2)
                    self.assertEqual(members[0].removesuffix(".png"), members[1].removesuffix(".json"))
                    self.assertEqual(archive.extractfile(members[0]).read(), PNG)
                    metadata = json.load(archive.extractfile(members[1]))
                    self.assertEqual(metadata["url"], page["url"])
                    self.assertEqual(metadata["entry_url"], candidate["url"])
                    self.assertEqual(metadata["category_path"], "News/Media")
                    self.assertEqual(metadata["hierarchy"], hierarchy)
            self.assertIn("path: data/train-*.tar", (output / "README.md").read_text())
            self.assertEqual(len((output / "samples.jsonl").read_text().splitlines()), 2)
            with self.assertRaisesRegex(ValueError, "empty"):
                pack_dataset(source, output)

    def test_batch_index_and_nodes_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input"
            folder, _ = fixture(source)
            index = {"config": {"sources": [{"id": "site", "baseUrl": "https://example.com/", "category": "Content"}],
                                "pages": [{"id": "example", "source": "site", "path": "/entry"}]},
                     "pages": [{"id": "example", "source": "site", "url": "https://example.com/page",
                                "status": "captured", "observations": [{"viewport": "desktop", "status": "captured",
                                                                         "directory": "pages/example/desktop"}]}]}
            (source / "batch-index.json").write_text(json.dumps(index))
            pack_dataset(source, root / "output", require_url=True)
            record = json.loads((root / "output/samples.jsonl").read_text())
            self.assertEqual(record["entry_url"], "https://example.com/entry")
            self.assertEqual(record["url_source"], "batch-index.json")
            (source / "batch-index.json").unlink()
            (folder / "nodes.json").write_text(json.dumps({"url": "https://example.com/fallback"}))
            pack_dataset(source, root / "fallback", require_url=True)
            self.assertEqual(json.loads((root / "fallback/samples.jsonl").read_text())["url_source"], "nodes.json")

    def test_missing_url_and_dimension_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder, _ = fixture(root / "input")
            with self.assertRaisesRegex(ValueError, "Missing page URL"):
                pack_dataset(root / "input", root / "strict", require_url=True)
            result = pack_dataset(root / "input", root / "allowed")
            self.assertEqual(result["missing_url"], 1)
            hierarchy = json.loads((folder / "hierarchy.json").read_text())
            hierarchy["capture"]["width"] = 10
            (folder / "hierarchy.json").write_text(json.dumps(hierarchy))
            with self.assertRaisesRegex(ValueError, "dimensions differ"):
                pack_dataset(root / "input", root / "bad")
            self.assertFalse((root / "bad/manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
