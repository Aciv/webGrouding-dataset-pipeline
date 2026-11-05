"""Boundary and round-trip tests using tiny independent PNG fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from page_filter.cli import filter_pages
from page_filter.rules import Rules, inspect_geometry


def label(count=10):
    """Use arbitrary overlapping boxes; overlap and layer depth are not invalidity criteria."""
    return {"capture": {"width": 100, "height": 100, "image": "full.png"},
            "nodes": [{"node_id": f"n{i}", "layer": i % 5, "bbox": [2, 2, 20, 10],
                       "layout_bbox": [2, 2, 20, 10]} for i in range(count)]}


def png_bytes(width=100, height=100):
    """Create a valid RGB PNG without image libraries or external fixtures."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff" * width) * height)) + chunk(b"IEND", b""))


def write_page(root, name, labels=None):
    """Write one three-size fixture with independently selectable annotations."""
    for viewport in ("desktop", "mobile", "tablet"):
        folder = root / "pages" / name / viewport
        folder.mkdir(parents=True, exist_ok=True)
        data = labels.get(viewport, label()) if labels else label()
        (folder / "hierarchy.json").write_text(json.dumps(data))
        (folder / "full.png").write_bytes(png_bytes())


class GeometryTests(unittest.TestCase):
    def test_exact_ten_is_valid_all_layers_counted(self):
        self.assertTrue(inspect_geometry(label(10), Rules())["valid"])
        self.assertEqual(inspect_geometry(label(9), Rules())["reasons"], ["too_few_boxes"])

    def test_fractional_tolerance_and_immutable_input(self):
        data = label()
        for node in data["nodes"]:
            node["bbox"] = [-1.9, 2, 103.8, 10]
        original = copy.deepcopy(data)
        self.assertTrue(inspect_geometry(data, Rules())["valid"])
        self.assertEqual(data, original)

    def test_both_count_and_ratio_are_required(self):
        for count, bad, expected in [(10, 4, True), (25, 5, False), (100, 5, True)]:
            data = label(count)
            for node in data["nodes"][:bad]:
                node["bbox"] = [95, 2, 20, 10]
            self.assertEqual(inspect_geometry(data, Rules())["valid"], expected)

    def test_clipped_bbox_can_still_expose_horizontal_layout_failure(self):
        data = label(20)
        for node in data["nodes"][:5]:
            node.update(bbox=[0, 2, 100, 10], layout_bbox=[0, 2, 200, 10])
        result = inspect_geometry(data, Rules())
        self.assertFalse(result["valid"])
        self.assertEqual(result["training_outside_boxes"], 0)
        self.assertEqual(result["layout_horizontal_outside"], 5)
        self.assertTrue(inspect_geometry(data, Rules(check_layout_overflow=False))["valid"])

    def test_layout_policy_can_warn_or_ignore_clean_labels(self):
        data = label(20)
        for node in data["nodes"][:5]:
            node.update(bbox=[0, 2, 100, 10], layout_bbox=[0, 2, 200, 10])
        rejected = inspect_geometry(data, Rules())
        self.assertFalse(rejected["valid"])
        warning = inspect_geometry(data, Rules(layout_overflow_policy="warn"))
        self.assertTrue(warning["valid"])
        self.assertEqual(warning["layout_horizontal_outside"], 5)
        self.assertEqual(warning["layout_warnings"], 5)
        ignored = inspect_geometry(data, Rules(layout_overflow_policy="ignore"))
        self.assertTrue(ignored["valid"])
        self.assertEqual(ignored["layout_warnings"], 0)
        self.assertEqual(ignored["layout_horizontal_outside"], 5)

    def test_normal_bottom_truncation_and_small_horizontal_loss_are_not_failures(self):
        data = label()
        for node in data["nodes"]:
            node["layout_bbox"] = [0, 0, 105, 8000]
        self.assertTrue(inspect_geometry(data, Rules())["valid"])

    def test_missing_layout_is_unknown_not_failure(self):
        data = label()
        for node in data["nodes"]:
            del node["layout_bbox"]
        result = inspect_geometry(data, Rules())
        self.assertTrue(result["valid"])
        self.assertEqual(result["layout_missing"], 10)

    def test_invalid_box_and_nonfinite_dimensions(self):
        for box in ([0, 0, -1, 10], [0, 0, float("nan"), 10], [0, 0, True, 10]):
            data = label(); data["nodes"][0]["bbox"] = box
            self.assertIn("invalid_bbox_values", inspect_geometry(data, Rules())["reasons"])
        data = label(); data["capture"]["width"] = float("inf")
        with self.assertRaises(ValueError):
            inspect_geometry(data, Rules())


class ManifestTests(unittest.TestCase):
    def test_multiresolution_rejects_whole_page_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_page(root, "good")
            write_page(root, "few", {"mobile": label(9)})
            write_page(root, "missing")
            (root / "pages/missing/tablet/full.png").unlink()
            write_page(root, "wrong_size")
            (root / "pages/wrong_size/tablet/full.png").write_bytes(png_bytes(101, 100))
            # Corrupt JSON belongs to this page only, not a fatal batch error.
            write_page(root, "bad_json")
            (root / "pages/bad_json/mobile/hierarchy.json").write_text("invalid-json")
            hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / "pages").rglob("*") if p.is_file()}
            report = filter_pages([root, root / "pages"], root / "valid.json", Rules(), ["desktop", "mobile", "tablet"], workers=2)
            self.assertEqual(report["summary"]["pages"], 5)
            self.assertEqual(report["summary"]["valid_pages"], 1)
            self.assertEqual(report["valid_pages"][0]["id"], "good")
            self.assertIn("mobile:too_few_boxes", next(p for p in report["rejected_pages"] if p["id"] == "few")["reasons"])
            for p, original in hashes.items():
                self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), original)
            with self.assertRaises(ValueError):
                filter_pages([root], root / "valid.json", Rules(), ["desktop"])
            with self.assertRaises(ValueError):
                filter_pages([root], root / "collection-index.json", Rules(), ["desktop"])

    def test_index_metadata_and_hidden_work_are_not_mixed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_page(root, "published")
            write_page(root / ".work/0001", "inflight")
            index = {"schema": "curlie_page_target_v1", "batches": [{"candidates": [
                {"id": "source", "url": "https://example.test/"}], "pages": [
                    {"id": "published", "source": "source", "url": "https://example.test/about", "category": "Science",
                     "category_path": "Science/Biology", "viewports": {vp: {
                         "hierarchy": f"pages/published/{vp}/hierarchy.json", "image": f"pages/published/{vp}/full.png"}
                         for vp in ("desktop", "mobile", "tablet")}}]}]}
            (root / "collection-index.json").write_text(json.dumps(index))
            report = filter_pages([root], root / "valid.json", Rules(), ["desktop", "mobile", "tablet"])
            self.assertEqual(report["summary"]["pages"], 1)
            self.assertEqual(report["valid_pages"][0]["category"], "Science")
            self.assertEqual(report["valid_pages"][0]["url"], "https://example.test/about")
            image = report["valid_pages"][0]["viewports"]["mobile"]["image"]
            self.assertTrue((Path(report["valid_pages"][0]["input_root"]) / image).is_file())


if __name__ == "__main__":
    unittest.main()
