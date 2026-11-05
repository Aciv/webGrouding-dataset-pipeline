"""End-to-end index pipeline tests using tiny independent PNG fixtures."""
import argparse
import contextlib
import hashlib
import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT = SCRIPTS.parent
for path in (SCRIPTS, PROJECT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from page_filter.rules import Rules
from pipeline.cli import command_run
from pipeline.filter import filter_index
from pipeline.index import build_index, load_index, sha256_file, write_index
from pipeline.postprocess import run_index
from hf_chunks.pack import pack_index


def label(count=10, url=None):
    """Parentless in-bounds boxes; enough nodes pass the default min_boxes threshold."""
    return {
        "capture": {"width": 100, "height": 100, "image": "full.png", "url": url},
        "nodes": [{"node_id": f"n{i}", "parent_id": None, "layer": 0,
                   "bbox": [2, 2, 20, 10], "layout_bbox": [2, 2, 20, 10]} for i in range(count)],
    }


def png_bytes(width=100, height=100):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff" * width) * height))
            + chunk(b"IEND", b""))


def write_page(root, page_id, count=10, viewports=("desktop",), url=None, size=(100, 100)):
    for viewport in viewports:
        folder = root / "pages" / page_id / viewport
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "hierarchy.json").write_text(json.dumps(label(count, url or f"https://example.test/{page_id}")))
        (folder / "full.png").write_bytes(png_bytes(*size))


def hashes(root):
    return {path: sha256_file(path) for path in Path(root).rglob("*") if path.is_file()}


def fake_processor(file, source, output, config, dry_run, reuse_edits=None,
                   image_override=None, relative_override=None, copy_image=True):
    """Write an accepted fused tree without an API call; mimics the postprocess contract."""
    destination = Path(output) / Path(relative_override).parent
    destination.mkdir(parents=True, exist_ok=True)
    tree = json.loads(Path(file).read_text(encoding="utf-8"))
    (destination / "fused-hierarchy.json").write_text(json.dumps(tree), encoding="utf-8")
    (destination / "layer-review.json").write_text(json.dumps({
        "status": "accepted", "postprocess_version": "visual_layers_fusion_clean5_v13",
        "source": str(file)}), encoding="utf-8")
    return {"status": "accepted", "source": str(file)}


class PipelineTests(unittest.TestCase):
    def fixture(self, temporary):
        root = Path(temporary)
        first, second = root / "run-a", root / "run-b"
        write_page(first, "a1")
        write_page(second, "b1")
        write_page(second, "b2", count=1)
        return root, first, second

    def filtered(self, temporary, roots, viewports=("desktop",)):
        index = build_index(list(roots), list(viewports), workers=2)
        index, summary = filter_index(index, Rules(), list(viewports), workers=2, stage="filtered")
        path = write_index(Path(temporary) / "index-filtered.json", index)
        return path, index, summary

    def test_multirun_filter_is_read_only_and_reports_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            before = {**hashes(first), **hashes(second)}
            path, index, summary = self.filtered(temporary, (first, second))
            self.assertEqual([run["id"] for run in index["runs"]], ["run-000", "run-001"])
            self.assertEqual(summary["pages"], 3)
            self.assertEqual(summary["valid_pages"], 2)
            self.assertEqual(summary["reasons"], {"desktop:too_few_boxes": 1})
            self.assertEqual(index["pages"][0]["url"], "https://example.test/a1")
            self.assertEqual(index["pages"][0]["status"], "valid")
            self.assertEqual(index["pages"][-1]["status"], "rejected")
            self.assertEqual(before, {**hashes(first), **hashes(second)})
            self.assertTrue(path.is_file())
            self.assertEqual(load_index(path)["stage"], "filtered")

    def test_declared_image_size_and_required_viewports_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_page(root / "run-a", "bad-size", size=(101, 100))
            write_page(root / "run-b", "only-desktop")
            index = build_index([root / "run-a", root / "run-b"],
                                ["desktop", "mobile", "tablet"])
            index, summary = filter_index(index, Rules(), ["desktop", "mobile", "tablet"],
                                          workers=2, stage="filtered")
            self.assertEqual(summary["valid_pages"], 0)
            reasons = [page["reasons"] for page in index["pages"]]
            self.assertTrue(any("desktop:image_size_mismatch" in entry for entry in reasons))
            self.assertTrue(any("mobile:missing_viewport" in entry and "tablet:missing_viewport" in entry
                                for entry in reasons))

    def test_postprocess_final_filter_and_index_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            before = {**hashes(first), **hashes(second)}
            filtered_path, _, _ = self.filtered(temporary, (first, second))
            post_index, summary = run_index(
                filtered_path, Path(temporary) / "index-post.json",
                Path(temporary) / "hierarchies", Path(temporary) / "processing",
                {"concurrency": 2}, processor=fake_processor)
            self.assertEqual(summary["statuses"], {"accepted": 2})
            self.assertEqual(post_index["stage"], "postprocessed")
            self.assertEqual(post_index["parent"]["sha256"], sha256_file(filtered_path))
            for page in post_index["pages"]:
                if page["status"] == "valid":
                    self.fail("filtered pages must wait for final filtering")
                if page["status"] == "rejected":
                    continue
                record = page["viewports"]["desktop"]
                self.assertEqual(record["hierarchy_side"], "derived")
                self.assertEqual(record["postprocess"]["status"], "accepted")
                derived = Path(post_index["hierarchies_root"]) / record["hierarchy"]
                self.assertTrue(derived.is_file())
                self.assertFalse((derived.parent / "full.png").exists())
            post_path = write_index(Path(temporary) / "index-post.json", post_index)
            final_index = load_index(post_path)
            final_index, final_summary = filter_index(final_index, Rules(), ["desktop"],
                                                      workers=2, stage="final")
            self.assertEqual(final_summary["valid_pages"], 2)
            self.assertEqual(final_summary["stage"], "final")
            final_path = write_index(Path(temporary) / "index-final.json", final_index)
            manifest_path = Path(temporary) / "chunks"
            manifest = pack_index(final_path, manifest_path, chunk_size_mb=1, dedupe_by="none")
            self.assertEqual(manifest["samples"], 2)
            self.assertEqual(manifest["hierarchy_policy"], "derived_clean_hierarchy")
            self.assertEqual(manifest["pipeline_index"]["stage"], "final")
            self.assertEqual(manifest["pipeline_index"]["skipped_pages"], 1)
            self.assertTrue(manifest["archive_prefix"])
            rows = [json.loads(line) for line in (manifest_path / "metadata.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            for row in rows:
                viewport = row["viewports"]["desktop"]
                self.assertEqual(viewport["hierarchy_source"], "derived")
                self.assertIn("derived_hierarchy_path", viewport)
                self.assertTrue(row["source_url"].startswith("https://example.test/"))
                with __import__("tarfile").open(manifest_path / row["chunk"]) as archive:
                    image_bytes = archive.extractfile(viewport["image_member"]).read()
                    derived_bytes = archive.extractfile(viewport["hierarchy_member"]).read()
                page = next(page for page in final_index["pages"] if page["page_id"] == row["page_id"])
                image = first / page["viewports"]["desktop"]["image"] if row["run_id"] == "run-000" \
                    else second / page["viewports"]["desktop"]["image"]
                self.assertEqual(image_bytes, image.read_bytes())
                derived_file = Path(final_index["hierarchies_root"]) / page["viewports"]["desktop"]["hierarchy"]
                self.assertEqual(derived_bytes, derived_file.read_bytes())
                self.assertIn("pages", viewport["hierarchy_member"])
                self.assertTrue(viewport["hierarchy_member"].endswith("hierarchy.json"))
            self.assertEqual(before, {**hashes(first), **hashes(second)})

    def test_postprocess_resume_reuses_accepted_and_retries_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            filtered_path, _, _ = self.filtered(temporary, (first, second))
            state = {"fail": True}
            calls = []

            def flaky_processor(file, source, output, config, dry_run, reuse_edits=None,
                                image_override=None, relative_override=None, copy_image=True):
                calls.append(Path(file).parent.parent.name)
                if Path(file).parent.parent.name == "b1" and state["fail"]:
                    return {"status": "failed", "error": "transient"}
                destination = Path(output) / Path(relative_override).parent
                destination.mkdir(parents=True, exist_ok=True)
                tree = json.loads(Path(file).read_text(encoding="utf-8"))
                (destination / "fused-hierarchy.json").write_text(json.dumps(tree), encoding="utf-8")
                (destination / "layer-review.json").write_text(json.dumps({
                    "status": "accepted",
                    "postprocess_version": "visual_layers_fusion_clean5_v13",
                    "source": str(file)}), encoding="utf-8")
                return {"status": "accepted"}

            first_index, first_summary = run_index(
                filtered_path, Path(temporary) / "post-1.json",
                Path(temporary) / "hier", Path(temporary) / "proc",
                {"concurrency": 1}, processor=flaky_processor)
            self.assertEqual(first_summary["statuses"], {"accepted": 1, "failed": 1})
            state["fail"] = False
            calls.clear()
            resumed_index, resumed_summary = run_index(
                filtered_path, Path(temporary) / "post-2.json",
                Path(temporary) / "hier", Path(temporary) / "proc",
                {"concurrency": 1}, resume=True, processor=flaky_processor)
            self.assertEqual(resumed_summary["statuses"], {"accepted": 2})
            self.assertEqual(calls, ["b1"])
            by_id = {page["page_id"]: page for page in resumed_index["pages"]}
            self.assertTrue(by_id["a1"]["viewports"]["desktop"]["postprocess"]["resumed"])
            self.assertFalse(by_id["b1"]["viewports"]["desktop"]["postprocess"]["resumed"])

    def test_run_resume_skips_completed_stages_and_retries_only_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            config = Path(temporary) / "config.json"
            config.write_text(json.dumps({"concurrency": 1}), encoding="utf-8")
            state = {"fail": True}
            calls = []

            def flaky(file, source, output, config, dry_run, reuse_edits=None,
                      image_override=None, relative_override=None, copy_image=True):
                page = Path(file).parent.parent.name
                calls.append(page)
                if page == "b1" and state["fail"]:
                    return {"status": "failed", "error": "transient"}
                destination = Path(output) / Path(relative_override).parent
                destination.mkdir(parents=True, exist_ok=True)
                tree = json.loads(Path(file).read_text(encoding="utf-8"))
                (destination / "fused-hierarchy.json").write_text(json.dumps(tree), encoding="utf-8")
                (destination / "layer-review.json").write_text(json.dumps({
                    "status": "accepted",
                    "postprocess_version": "visual_layers_fusion_clean5_v13",
                    "source": str(file)}), encoding="utf-8")
                return {"status": "accepted"}

            def patched(*args, **kwargs):
                kwargs["processor"] = flaky
                return run_index(*args, **kwargs)

            def namespace(resume, stop_after="pack"):
                return argparse.Namespace(
                    input=[first, second], workdir=Path(temporary) / "work", config=config,
                    pack_output=None, limit=0, concurrency=1, resume=resume,
                    viewports=["desktop"], min_boxes=10, min_outside_boxes=5,
                    max_outside_ratio=0.2, tolerance_px=2, check_layout_overflow=True,
                    min_layout_overflow_ratio=0.1, workers=2, chunk_size_mb=1,
                    dedupe_by="none", seed=42, require_url=False, pack_workers=1,
                    pixel_concurrency=None, stop_after=stop_after)

            with mock.patch("pipeline.postprocess.run_index", side_effect=patched):
                with contextlib.redirect_stdout(io.StringIO()):
                    command_run(namespace(False))
                self.assertEqual(calls, ["a1", "b1"])
                work = Path(temporary) / "work"
                post = json.loads((work / "index-postprocessed.json").read_text())
                final = json.loads((work / "index-final.json").read_text())
                self.assertTrue(post["parent"]["path"].endswith("index-filtered.json"))
                self.assertTrue(final["parent"]["path"].endswith("index-postprocessed.json"))
                calls.clear()
                state["fail"] = False
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    command_run(namespace(True))
            self.assertEqual(calls, ["b1"])
            text = output.getvalue()
            self.assertIn('"stage": "filter", "status": "skipped"', text)
            self.assertIn('"stage": "postprocess", "status": "resuming"', text)
            self.assertIn('"status": "new_output"', text)
            packages = sorted(path.name for path in (Path(temporary) / "work").iterdir()
                              if path.is_dir() and path.name.startswith("chunks"))
            self.assertEqual(packages, ["chunks", "chunks-2"])
            manifest = json.loads((Path(temporary) / "work" / "chunks-2" / "manifest.json").read_text())
            self.assertEqual(manifest["pages"], 2)

    def test_run_can_stop_after_postprocess_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            config = Path(temporary) / "config.json"
            config.write_text(json.dumps({"concurrency": 1}), encoding="utf-8")
            calls = []

            def recording(file, source, output, config, dry_run, reuse_edits=None,
                          image_override=None, relative_override=None, copy_image=True):
                calls.append(Path(file).parent.parent.name)
                destination = Path(output) / Path(relative_override).parent
                destination.mkdir(parents=True, exist_ok=True)
                tree = json.loads(Path(file).read_text(encoding="utf-8"))
                (destination / "fused-hierarchy.json").write_text(json.dumps(tree), encoding="utf-8")
                (destination / "layer-review.json").write_text(json.dumps({
                    "status": "accepted",
                    "postprocess_version": "visual_layers_fusion_clean5_v13",
                    "source": str(file)}), encoding="utf-8")
                return {"status": "accepted"}

            def patched(*args, **kwargs):
                kwargs["processor"] = recording
                return run_index(*args, **kwargs)

            def namespace(resume, stop_after):
                return argparse.Namespace(
                    input=[first, second], workdir=Path(temporary) / "work", config=config,
                    pack_output=None, limit=0, concurrency=1, resume=resume,
                    viewports=["desktop"], min_boxes=10, min_outside_boxes=5,
                    max_outside_ratio=0.2, tolerance_px=2, check_layout_overflow=True,
                    min_layout_overflow_ratio=0.1, workers=2, chunk_size_mb=1,
                    dedupe_by="none", seed=42, require_url=False, pack_workers=1,
                    pixel_concurrency=None, stop_after=stop_after)

            with mock.patch("pipeline.postprocess.run_index", side_effect=patched):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    command_run(namespace(False, "postprocess"))
                self.assertEqual(calls, ["a1", "b1"])
                work = Path(temporary) / "work"
                self.assertTrue((work / "index-filtered.json").is_file())
                self.assertTrue((work / "index-postprocessed.json").is_file())
                self.assertFalse((work / "index-final.json").exists())
                self.assertFalse((work / "chunks").exists())
                self.assertIn('"stage": "postprocess"', output.getvalue())
                calls.clear()
                with contextlib.redirect_stdout(io.StringIO()):
                    command_run(namespace(True, "pack"))
            self.assertEqual(calls, [])
            self.assertTrue((work / "index-final.json").is_file())
            manifest = json.loads((work / "chunks" / "manifest.json").read_text())
            self.assertEqual(manifest["pages"], 2)

    def test_parallel_pack_writes_all_pages_with_worker_shards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            filtered_path, _, _ = self.filtered(temporary, (first, second))
            output = Path(temporary) / "chunks"
            manifest = pack_index(filtered_path, output, chunk_size_mb=1,
                                  dedupe_by="none", workers=2)
            self.assertEqual(manifest["pack_workers"], 2)
            self.assertEqual(manifest["samples"], 2)
            self.assertEqual(len(manifest["shards"]), 2)
            self.assertTrue(all("/train-0" in shard["file"] for shard in manifest["shards"]))
            rows = [json.loads(line) for line in (output / "metadata.jsonl").read_text().splitlines()]
            self.assertEqual([row["index"] for row in rows], [0, 1])
            for row in rows:
                with __import__("tarfile").open(output / row["chunk"]) as archive:
                    for viewport in row["viewports"].values():
                        self.assertIn(viewport["image_member"], archive.getnames())
                        self.assertIn(viewport["hierarchy_member"], archive.getnames())

    def test_pack_resume_reuses_a_matching_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            filtered_path, _, _ = self.filtered(temporary, (first, second))
            post_index, _ = run_index(
                filtered_path, Path(temporary) / "index-post.json",
                Path(temporary) / "hierarchies", Path(temporary) / "processing",
                {"concurrency": 2}, processor=fake_processor)
            post_path = write_index(Path(temporary) / "index-post.json", post_index)
            final_index, _ = filter_index(load_index(post_path), Rules(), ["desktop"],
                                          workers=2, stage="final")
            final_path = write_index(Path(temporary) / "index-final.json", final_index)
            output = Path(temporary) / "chunks"
            first_pack = pack_index(final_path, output, chunk_size_mb=1, dedupe_by="none")
            resumed_pack = pack_index(final_path, output, chunk_size_mb=1,
                                      dedupe_by="none", resume=True)
            self.assertTrue(resumed_pack["resumed"])
            self.assertEqual(first_pack["shards"], resumed_pack["shards"])
            with self.assertRaises(ValueError):
                pack_index(final_path, output, chunk_size_mb=2, dedupe_by="none", resume=True)

    def test_final_filter_rejects_tampered_derived_hierarchy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            filtered_path, _, _ = self.filtered(temporary, (first, second))
            post_index, _ = run_index(
                filtered_path, Path(temporary) / "index-post.json",
                Path(temporary) / "hierarchies", Path(temporary) / "processing",
                {"concurrency": 2}, processor=fake_processor)
            page = next(page for page in post_index["pages"] if page["status"] == "pending_final_filter")
            derived = Path(post_index["hierarchies_root"]) / page["viewports"]["desktop"]["hierarchy"]
            derived.write_text(json.dumps(label(count=1)), encoding="utf-8")
            post_path = write_index(Path(temporary) / "index-post.json", post_index)
            final_index, summary = filter_index(load_index(post_path), Rules(), ["desktop"],
                                                workers=2, stage="final")
            reasons = [reason for page in final_index["pages"] if page["status"] == "rejected"
                       for reason in page["reasons"]]
            self.assertIn("desktop:hierarchy_sha256_mismatch", reasons)

    def test_authoritative_index_missing_hierarchy_is_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pages").mkdir()
            index = {
                "schema": "curlie_page_target_v1",
                "batches": [{
                    "candidates": [{"id": "source", "url": "https://example.test/"}],
                    "pages": [{
                        "id": "missing", "source": "source", "url": "https://example.test/missing",
                        "category": "Science",
                        "viewports": {"desktop": {
                            "hierarchy": "pages/missing/desktop/hierarchy.json",
                            "image": "pages/missing/desktop/full.png"}}}]}],
            }
            (root / "collection-index.json").write_text(json.dumps(index), encoding="utf-8")
            built = build_index([root], ["desktop"])
            self.assertEqual(built["pages"][0]["url"], "https://example.test/missing")
            self.assertEqual(built["pages"][0]["category"], "Science")
            built, summary = filter_index(built, Rules(), ["desktop"], workers=1, stage="filtered")
            self.assertEqual(summary["valid_pages"], 0)
            self.assertIn("desktop:missing_or_invalid_training_pair", built["pages"][0]["reasons"])

    def test_final_filter_rechecks_derived_geometry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            filtered_path, _, _ = self.filtered(temporary, (first, second))
            post_index, _ = run_index(
                filtered_path, Path(temporary) / "index-post.json",
                Path(temporary) / "hierarchies", Path(temporary) / "processing",
                {"concurrency": 2}, processor=fake_processor)
            page = next(page for page in post_index["pages"] if page["status"] == "pending_final_filter")
            record = page["viewports"]["desktop"]
            derived = Path(post_index["hierarchies_root"]) / record["hierarchy"]
            derived.write_text(json.dumps(label(count=1)), encoding="utf-8")
            record["hierarchy_sha256"] = sha256_file(derived)
            post_path = write_index(Path(temporary) / "index-post.json", post_index)
            final_index, _ = filter_index(load_index(post_path), Rules(), ["desktop"],
                                          workers=2, stage="final")
            reasons = [reason for page in final_index["pages"] if page["status"] == "rejected"
                       for reason in page["reasons"]]
            self.assertIn("desktop:too_few_boxes", reasons)

    def test_cli_filter_builds_a_multi_root_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, first, second = self.fixture(temporary)
            output = Path(temporary) / "index.json"
            command = [sys.executable, str(SCRIPTS / "pipeline" / "cli.py"), "filter",
                       "--input", str(first), str(second), "--output", str(output),
                       "--viewports", "desktop", "--workers", "2"]
            result = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"valid_pages": 2', result.stdout)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
