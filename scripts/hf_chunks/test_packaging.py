"""Local checks for compressed shards, URL indexes and atomic publication."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pack import dedupe_pages, normalize_page_url, pack_dataset

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=")


def write_pair(source, page, viewport, url, width=1, height=1):
    """Create one canonical training pair with a diagnostic file that must not be packed."""
    folder = source / "pages" / page / viewport
    folder.mkdir(parents=True, exist_ok=True)
    hierarchy = {"schema": "visual_hierarchy_candidate_v1",
                 "rule_version": "test_rules",
                 "annotation_status": "rule_generated_unverified",
                 "capture": {"image": "full.png", "width": width, "height": height,
                             "capturedAt": "2026-11-04T00:00:00Z", **({"url": url} if url else {})},
                 "nodes": []}
    (folder / "full.png").write_bytes(PNG)
    (folder / "overlay.png").write_bytes(b"do not pack")
    (folder / "hierarchy.json").write_text(json.dumps(hierarchy))
    return folder


def index(source, pages):
    """Write a Curlie-style root index whose viewport paths point at the fixtures."""
    batches = []
    for batch_number, entries in enumerate((pages[:1], pages[1:])):
        if not entries:
            continue
        candidates = {page["source"]: {"id": page["source"], "url": page["entry"],
                                       "host": page["source"], "category": page["category"],
                                       "category_path": f"{page['category']}/Test"}
                      for page in entries}
        batches.append({"id": f"{batch_number:05d}", "candidates": list(candidates.values()),
                        "pages": [{"id": page["page"], "source": page["source"], **page["meta"],
                                   "viewports": {viewport: {
                                       "image": f"pages/{page['page']}/{viewport}/full.png",
                                       "hierarchy": f"pages/{page['page']}/{viewport}/hierarchy.json"}
                                       for viewport in page["viewports"]}}
                                  for page in entries]})
    (source / "collection-index.json").write_text(json.dumps({
        "schema": "curlie_page_target_v1", "batches": batches}))


def run(root, pages, hidden=False):
    """Create one collection run with complete three-viewport pages."""
    source = root / "input"
    for page in pages:
        for viewport in page["viewports"]:
            write_pair(source, page["page"], viewport, page["meta"]["url"])
    index(source, pages)
    if hidden:
        write_pair(source / ".work/00001", "hidden", "desktop", "https://hidden.example/")
    return source


def fixture(root):
    """Two sources / two pages / three viewport samples plus a hidden .work copy."""
    viewports = ["desktop", "mobile", "tablet"]
    pages = [
        {"page": "p1", "source": "site-1", "entry": "https://one.example/entry",
         "category": "News", "viewports": viewports,
         "meta": {"url": "https://one.example/article", "title": "One"}},
        {"page": "p2", "source": "site-2", "entry": "https://two.example/",
         "category": "Computers", "viewports": viewports,
         "meta": {"url": "https://two.example/", "title": "Two"}},
    ]
    return run(root, pages, hidden=True)


class PackingTests(unittest.TestCase):
    """No browser, network or non-standard dependency is required for these tests."""
    def test_gzip_chunks_metadata_and_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture(root)
            output = root / "output"
            result = pack_dataset(source, output, chunk_size_mb=0.0001, require_url=True)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["samples"], 2)
            self.assertEqual(result["pages"], 2)
            self.assertEqual(result["images"], 6)
            self.assertEqual(result["compression"], "gzip")
            self.assertEqual(result["missing_url"], 0)
            self.assertEqual(result["viewports_per_page"], {"3": 2})
            self.assertEqual(len(result["shards"]), 2)
            rows = [json.loads(line) for line in (output / "metadata.jsonl").read_text().splitlines()]
            self.assertEqual([row["index"] for row in rows], [0, 1])
            self.assertEqual([row["sample_id"] for row in rows], ["000000000", "000000001"])
            self.assertEqual({row["source_url"] for row in rows},
                             {"https://one.example/article", "https://two.example/"})
            self.assertTrue(all(row["source_url"] == row["url"] for row in rows))
            self.assertTrue(all(row["image_count"] == 3 for row in rows))
            self.assertNotIn("page_member", rows[0])
            for row in rows:
                chunk = output / row["chunk"]
                self.assertTrue(chunk.is_file())
                self.assertTrue(chunk.name.endswith(".tar.gz"))
                self.assertEqual(set(row["viewports"]), {"desktop", "mobile", "tablet"})
                with tarfile.open(chunk, "r:*") as archive:
                    members = archive.getnames()
                    self.assertEqual(len(members), 6)
                    for viewport, entry in row["viewports"].items():
                        expected_image = f"pages/{row['page_id']}/{viewport}/full.png"
                        expected_hierarchy = f"pages/{row['page_id']}/{viewport}/hierarchy.json"
                        self.assertEqual(entry["image_member"], expected_image)
                        self.assertEqual(entry["hierarchy_member"], expected_hierarchy)
                        self.assertEqual(entry["original_image_path"], expected_image)
                        self.assertEqual(entry["original_hierarchy_path"], expected_hierarchy)
                        self.assertIn(entry["image_member"], members,
                                      "all three resolutions must be in the same chunk")
                        self.assertIn(entry["hierarchy_member"], members)
                        image = archive.extractfile(entry["image_member"]).read()
                        hierarchy_blob = archive.extractfile(entry["hierarchy_member"]).read()
                        self.assertEqual(image, PNG)
                        self.assertEqual(hashlib.sha256(image).hexdigest(), entry["image_sha256"])
                        self.assertEqual(hierarchy_blob,
                                         (source / expected_hierarchy).read_bytes())
                        self.assertEqual(hashlib.sha256(hierarchy_blob).hexdigest(),
                                         entry["hierarchy_sha256"])
                        hierarchy = json.loads(hierarchy_blob)
                        self.assertEqual(hierarchy["rule_version"], "test_rules")
                        self.assertEqual(hierarchy["capture"]["url"], row["source_url"])
                    self.assertNotIn("overlay.png", " ".join(members))
            self.assertEqual(len({row["chunk"] for row in rows}), 2)
            extracted = root / "extracted"
            for row in rows:
                with tarfile.open(output / row["chunk"], "r:*") as archive:
                    archive.extractall(extracted, filter="data")
            actual = {path.relative_to(extracted).as_posix()
                      for path in extracted.rglob("*") if path.is_file()}
            expected = {f"pages/{page}/{viewport}/{name}"
                        for page in ("p1", "p2")
                        for viewport in ("desktop", "mobile", "tablet")
                        for name in ("full.png", "hierarchy.json")}
            self.assertEqual(actual, expected)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(len(manifest["shards"]), 2)
            self.assertEqual(manifest["viewports_per_page"], {"3": 2})
            self.assertEqual(manifest["archive_layout"], "original_published_tree")
            self.assertEqual(manifest["archive_prefix"], "")
            for shard in manifest["shards"]:
                data = (output / shard["file"]).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), shard["sha256"])
                self.assertEqual(len(data), shard["bytes"])
            card = (output / "README.md").read_text()
            self.assertIn("gzip-compressed", card)
            self.assertIn("source_url", card)
            self.assertIn("original", card)
            self.assertEqual(result["shards"], manifest["shards"])

    def test_normalize_page_url(self):
        left = normalize_page_url(
            "http://WWW.One.Example:80/a/index.html?utm_source=x&b=2&a=1#fragment")
        right = normalize_page_url("https://one.example/a/?b=2&a=1")
        self.assertEqual(left, right)
        self.assertEqual(left, "one.example/a/?a=1&b=2")
        self.assertNotEqual(normalize_page_url("https://one.example/other"),
                            normalize_page_url("https://one.example/a/"))

    def test_dedupe_report_names_final_winner(self):
        def page(page_id, run_id, count):
            return [{"run_id": run_id, "run_name": run_id,
                     "provenance": {"page_id": page_id, "viewport": f"v{index}",
                                    "url": "https://same.example/"}}
                    for index in range(count)]
        kept, duplicates = dedupe_pages([
            page("two", "run-000", 2), page("one", "run-001", 1), page("three", "run-002", 3)
        ], "url")
        self.assertEqual([item[0]["provenance"]["page_id"] for item in kept], ["three"])
        self.assertEqual(len(duplicates), 2)
        self.assertTrue(all(item["kept"]["page_id"] == "three" for item in duplicates))
        self.assertTrue(all(item["reason"] == "more_viewports" for item in duplicates))

    def test_multiple_runs_url_site_and_no_dedupe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_a = fixture(root / "a")
            viewports = ["desktop", "mobile", "tablet"]
            source_b = run(root / "b", [
                {"page": "dup", "source": "site-3", "entry": "http://www.one.example/",
                 "category": "News", "viewports": viewports,
                 "meta": {"url": "http://www.one.example/article?utm_source=run-b",
                          "title": "One duplicate"}},
                {"page": "three", "source": "site-4", "entry": "https://two.example/other",
                 "category": "Sports", "viewports": viewports,
                 "meta": {"url": "https://two.example/other", "title": "Other"}},
            ])
            url_output = root / "url-dedupe"
            result = pack_dataset([source_a, source_b], url_output, chunk_size_mb=10,
                                  require_url=True)
            self.assertEqual(len(result["inputs"]), 2)
            self.assertEqual(result["input_pages"], 4)
            self.assertEqual(result["input_images"], 12)
            self.assertEqual(result["pages"], 3)
            self.assertEqual(result["images"], 9)
            self.assertEqual(result["dedupe"],
                             {"by": "url", "kept_pages": 3, "dropped_pages": 1})
            self.assertEqual([item["kept_pages"] for item in result["inputs"]], [2, 1])
            rows = [json.loads(line) for line in
                    (url_output / "metadata.jsonl").read_text().splitlines()]
            self.assertEqual({row["run_id"] for row in rows}, {"run-000", "run-001"})
            self.assertTrue(all(row["run_name"] == "input" for row in rows))
            self.assertEqual(len({normalize_page_url(row["source_url"]) for row in rows}), 3)
            duplicates = [json.loads(line) for line in
                          (url_output / "duplicates.jsonl").read_text().splitlines()]
            self.assertEqual(len(duplicates), 1)
            self.assertEqual(duplicates[0]["reason"], "first_seen")
            self.assertEqual(duplicates[0]["key"], "one.example/article")
            self.assertEqual(duplicates[0]["kept"]["run_id"], "run-000")
            self.assertEqual(duplicates[0]["dropped"]["run_id"], "run-001")
            self.assertEqual(duplicates[0]["dropped"]["page_id"], "dup")
            self.assertIn("duplicates.jsonl", (url_output / "README.md").read_text())
            extracted = root / "url-extracted"
            for row in rows:
                with tarfile.open(url_output / row["chunk"], "r:*") as archive:
                    archive.extractall(extracted, filter="data")
            actual = {path.relative_to(extracted).as_posix()
                      for path in extracted.rglob("*") if path.is_file()}
            self.assertIn("run-000/pages/p1/desktop/full.png", actual)
            self.assertIn("run-000/pages/p1/desktop/hierarchy.json", actual)
            self.assertIn("run-001/pages/three/tablet/full.png", actual)
            self.assertFalse(any(path.startswith("run-001/pages/dup/") for path in actual))
            self.assertEqual(
                (extracted / "run-001/pages/three/desktop/hierarchy.json").read_bytes(),
                (source_b / "pages/three/desktop/hierarchy.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "Duplicate input"):
                pack_dataset([source_a, source_a], root / "duplicate-input")

            site_output = root / "site-dedupe"
            site = pack_dataset([source_a, source_b], site_output, chunk_size_mb=10,
                                dedupe_by="site", require_url=True)
            self.assertEqual(site["pages"], 2)
            self.assertEqual(site["images"], 6)
            self.assertEqual(site["dedupe"]["dropped_pages"], 2)
            site_rows = [json.loads(line) for line in
                         (site_output / "metadata.jsonl").read_text().splitlines()]
            self.assertEqual({row["page_id"] for row in site_rows}, {"p1", "p2"})
            site_duplicates = [json.loads(line) for line in
                               (site_output / "duplicates.jsonl").read_text().splitlines()]
            self.assertEqual({item["key"] for item in site_duplicates},
                             {"one.example", "two.example"})
            self.assertTrue(all(item["kept"]["run_id"] == "run-000" for item in site_duplicates))

            none_output = root / "no-dedupe"
            none = pack_dataset([source_a, source_b], none_output, chunk_size_mb=10,
                                dedupe_by="none", require_url=True)
            self.assertEqual(none["pages"], 4)
            self.assertEqual(none["images"], 12)
            self.assertEqual(none["dedupe"]["dropped_pages"], 0)
            self.assertFalse((none_output / "duplicates.jsonl").exists())
            self.assertEqual(len((none_output / "metadata.jsonl").read_text().splitlines()), 4)

    def test_missing_url_and_nonempty_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input"
            write_pair(source, "p1", "desktop", None)
            with self.assertRaisesRegex(ValueError, "Missing page URL"):
                pack_dataset(source, root / "strict", require_url=True)
            result = pack_dataset(source, root / "allowed")
            self.assertEqual(result["missing_url"], 1)
            row = json.loads((root / "allowed/metadata.jsonl").read_text())
            self.assertIsNone(row["source_url"])
            self.assertEqual(row["url_source"], "missing")
            self.assertEqual(set(row["viewports"]), {"desktop"})
            self.assertEqual(row["viewports"]["desktop"]["image_member"],
                             "pages/p1/desktop/full.png")
            self.assertEqual(row["viewports"]["desktop"]["hierarchy_member"],
                             "pages/p1/desktop/hierarchy.json")
            with self.assertRaisesRegex(ValueError, "empty"):
                pack_dataset(source, root / "allowed")


if __name__ == "__main__":
    unittest.main()
