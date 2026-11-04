"""Deterministic tree and mock vision API regression tests; never calls a paid service."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from rules import apply_operations, propose, validate
from legacy_pipeline import process_legacy as process, PIPELINE_VERSION
from normalize import normalize
from layout import layout_operations, partition
from vlm import parse_response
from model_edits import apply_model_edits
from frontier import large_region_requirements, validate_large_region_review, clipped_fragment_operations, bottom_up_operations


def node(key, parent=None, box=None):
    return {"node_id": key, "parent_id": parent, "layer": 99, "bbox": box or [0, 0, 100, 100],
        "struct": "block", "kind": "group", "disposition": "detection", "source_node_ids": [key], "reasons": []}


def tree(nodes):
    return {"capture": {"image": "full.png", "width": 100, "height": 100}, "nodes": nodes,
        "decisions": [], "ignore_regions": [], "stats": {"raw_visible": len(nodes)}}


def png(file):
    def chunk(kind, data):
        return struct.pack(">I", len(data))+kind+data+struct.pack(">I", zlib.crc32(kind+data))
    file.write_bytes(b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB", 100, 100, 8, 2, 0, 0, 0))+
        chunk(b"IDAT", zlib.compress((b"\x00"+b"\xff"*300)*100))+chunk(b"IEND", b""))


class RulesTest(unittest.TestCase):
    def test_contract_preserves_children_and_input(self):
        original = tree([node("p"), node("a", "p"), node("leaf", "a", [10, 10, 20, 20])])
        frozen = deepcopy(original)
        result, _ = apply_operations(original, [{"op": "contract", "node": "a", "reason": "wrapper"}])
        self.assertEqual(original, frozen)
        self.assertEqual(result["nodes"][1]["parent_id"], "p")
        self.assertEqual(result["nodes"][1]["layer"], 1)

    def test_merge_preserves_both_subtrees(self):
        original = tree([node("p"), node("a", "p"), node("b", "p"),
            node("x", "a", [1, 1, 10, 10]), node("y", "b", [20, 20, 10, 10])])
        result, _ = apply_operations(original, [{"op": "merge", "nodes": ["a", "b"], "keep": "a", "reason": "duplicate"}])
        self.assertEqual({n["parent_id"] for n in result["nodes"] if n["node_id"] in ("x", "y")}, {"a"})
        self.assertEqual(next(n for n in result["nodes"] if n["node_id"] == "a")["source_node_ids"], ["a", "b"])

    def test_drop_is_leaf_only_and_records_ignore(self):
        original = tree([node("p"), node("leaf", "p", [1, 1, 2, 2])])
        with self.assertRaises(ValueError):
            apply_operations(original, [{"op": "drop", "node": "p", "reason": "bad"}])
        result, _ = apply_operations(original, [{"op": "drop", "node": "leaf", "reason": "tiny_star"}])
        self.assertEqual(result["ignore_regions"][0]["bbox"], [1, 1, 2, 2])

    def test_repeated_applied_cleanup_is_idempotent(self):
        first, _ = apply_operations(tree([node("p"), node("leaf", "p", [1, 1, 2, 2])]), [
            {"op": "drop", "node": "leaf", "reason": "tiny"}])
        second, trail = apply_operations(first, [{"op": "drop", "node": "leaf", "reason": "repeated"}])
        self.assertEqual(len(second["ignore_regions"]), 1)
        self.assertEqual(trail[0]["outcome"], "already_omitted")

    def test_group_bbox_and_recomputed_depths(self):
        result, _ = apply_operations(tree([node("a", box=[0, 0, 10, 10]), node("b", box=[20, 0, 10, 10])]),
            [{"op": "group", "nodes": ["a", "b"], "id": "vlm_group_1", "reason": "card"}])
        self.assertEqual(result["nodes"][-1]["bbox"], [0, 0, 30, 10])
        self.assertEqual(validate(result, 4)["a"], 1)

    def test_cycle_and_cross_parent_merge_rejected(self):
        original = tree([node("p"), node("a", "p")])
        with self.assertRaises(ValueError):
            apply_operations(original, [{"op": "reparent", "node": "p", "parent": "a", "reason": "bad"}])
        with self.assertRaises(ValueError):
            apply_operations(original, [{"op": "merge", "nodes": ["p", "a"], "keep": "p", "reason": "bad"}])

    def test_max_depth_and_proposals(self):
        original = tree([node(str(i), str(i-1) if i else None) for i in range(5)])
        with self.assertRaises(ValueError):
            validate(original, 4)
        self.assertTrue(any(p["op"] == "contract" for p in propose(original, {})))

    def test_normalize_long_wrapper_chain_preserves_leaf(self):
        original = tree([node(str(i), str(i-1) if i else None,
            [i, i, 100-2*i, 100-2*i]) for i in range(7)])
        original["nodes"][-1]["struct"] = "image"
        result, edits, summary = normalize(original, {})
        self.assertIn("6", {n["node_id"] for n in result["nodes"]})
        self.assertLessEqual(summary["after_levels"], 4)
        self.assertTrue(edits)
        validate(result, 4)

    def test_overflow_details_are_ignored_not_depth_failure(self):
        original = tree([node(str(i), str(i-1) if i else None,
            [i*3, i*3, 100-6*i, 100-6*i]) for i in range(6)])
        for n in original["nodes"]:
            n["struct"] = "table"  # No generic unary wrappers to contract.
        result, _, summary = normalize(original, {})
        self.assertEqual(summary["after_levels"], 4)
        self.assertEqual({v["node_id"] for v in result["ignore_regions"]}, {"4", "5"})

    def test_duplicate_siblings_keep_children_during_normalize(self):
        original = tree([node("a", box=[0, 0, 80, 80]), node("b", box=[1, 1, 80, 80]),
            node("x", "a", [5, 5, 10, 10]), node("y", "b", [50, 50, 10, 10])])
        result, _, _ = normalize(original, {"layer1_max_area_ratio": .9})
        self.assertEqual({n["node_id"] for n in result["nodes"]}, {"a", "x", "y"})
        self.assertEqual({n["parent_id"] for n in result["nodes"] if n["node_id"] in ("x", "y")}, {"a"})

    def test_restore_rule_omission_removes_ignore(self):
        original = tree([node(str(i), str(i-1) if i else None,
            [i*3, i*3, 100-6*i, 100-6*i]) for i in range(5)])
        for n in original["nodes"]:
            n["struct"] = "table"
        candidate, _, _ = normalize(original, {})
        restored, _ = apply_operations(candidate, [{"op": "restore", "node": "4", "parent": "1", "reason": "important content"}])
        self.assertEqual(next(n["parent_id"] for n in restored["nodes"] if n["node_id"] == "4"), "1")
        self.assertNotIn("4", {v["node_id"] for v in restored["ignore_regions"]})
        validate(restored, 4)

    def test_existing_sections_replace_main_without_losing_children(self):
        original = tree([node("main"), node("s1", "main", [0, 0, 100, 40]), node("s2", "main", [0, 60, 100, 40]),
            node("image", "s1", [2, 2, 10, 10])])
        original["nodes"][0].update(struct="region", subtype="main")
        for n in original["nodes"][1:3]:
            n["subtype"] = "section"
        result, _, _ = normalize(original, {})
        self.assertEqual({n["node_id"] for n in result["nodes"] if n["parent_id"] is None}, {"s1", "s2"})
        self.assertIn("image", {n["node_id"] for n in result["nodes"]})

    def test_mixed_shell_regroups_before_promoting(self):
        original = tree([node("shell"), node("nav", "shell", [0, 0, 10, 100]),
            node("text", "shell", [40, 0, 20, 25]), node("portrait", "shell", [65, 0, 35, 25]),
            node("table", "shell", [40, 65, 60, 35])])
        original["nodes"][1]["struct"] = "list"
        original["nodes"][2]["struct"] = "text"
        original["nodes"][3]["struct"] = "image"
        original["nodes"][4]["struct"] = "table"
        operations, evidence = layout_operations(original, {"layout_min_gap": 20})
        result, _ = apply_operations(original, operations)
        self.assertEqual(len(evidence), 1)
        self.assertNotIn("shell", {n["node_id"] for n in result["nodes"]})
        text_parent = next(n["parent_id"] for n in result["nodes"] if n["node_id"] == "text")
        self.assertEqual(text_parent, next(n["parent_id"] for n in result["nodes"] if n["node_id"] == "portrait"))
        self.assertNotEqual(text_parent, next(n["parent_id"] for n in result["nodes"] if n["node_id"] == "nav"))

    def test_full_page_image_and_table_not_split(self):
        for cls in ("image", "table"):
            original = tree([node("p"), node("x", "p", [0, 0, 10, 10]), node("y", "p", [80, 80, 10, 10])])
            original["nodes"][0]["struct"] = cls
            self.assertEqual(layout_operations(original, {})[0], [])

    def test_split_never_cuts_spanning_child(self):
        frontier = [node("wide", box=[0, 0, 100, 100]), node("left", box=[0, 0, 10, 10]), node("right", box=[90, 90, 10, 10])]
        self.assertEqual(len(partition(frontier, {})), 1)

    def test_model_group_alias_and_cycle_rejection(self):
        original = tree([node("p"), node("a", "p", [0, 0, 10, 10]), node("b", "p", [30, 0, 10, 10])])
        result, applied, rejected = apply_model_edits(original, [
            {"op": "group", "nodes": ["a", "b"], "id": "footer_links", "reason": "related"},
            {"op": "reparent", "node": "p", "parent": "footer_links", "reason": "invalid direction"}])
        self.assertEqual(len(applied), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIn("Cyclic", rejected[0]["error"])
        self.assertEqual(next(n["parent_id"] for n in result["nodes"] if n["node_id"] == "a"), "vlm_group_footer_links")
        validate(result)

    def test_generated_group_geometry_tracks_reparenting(self):
        original = tree([node("a", box=[0, 0, 10, 10]), node("b", box=[30, 0, 10, 10]), node("c", box=[80, 0, 10, 10])])
        grouped, _ = apply_operations(original, [{"op": "group", "nodes": ["a", "b", "c"], "id": "vlm_group_1", "reason": "group"}])
        result, _ = apply_operations(grouped, [{"op": "reparent", "node": "c", "parent": None, "reason": "separate region"}])
        self.assertEqual(next(n["bbox"] for n in result["nodes"] if n["node_id"] == "vlm_group_1"), [0, 0, 40, 10])

    def test_area_gate_only_applies_to_final_roots(self):
        original = tree([node("region", box=[0, 0, 70, 70]), node("table", "region", [0, 0, 65, 65])])
        self.assertEqual(large_region_requirements(original, {}), [])  # Child occupies 86% of parent, exempt from L1 gate.

    def test_oversized_atomic_root_explicitly_exempted(self):
        original = tree([node("image")])
        original["nodes"][0]["struct"] = "image"
        checks = large_region_requirements(original, {})
        self.assertEqual(checks[0]["status"], "exempt_atomic")
        self.assertTrue(validate_large_region_review(original, {}, {"accepted": True})[0])

    def test_large_container_cannot_pass_with_bare_accepted(self):
        original = tree([node("main")])
        self.assertFalse(validate_large_region_review(original, {}, {"accepted": True})[0])
        self.assertTrue(validate_large_region_review(original, {}, {"accepted": True, "large_region_decisions": [
            {"node_id": "main", "decision": "keep_single_component", "reason": "single indivisible visual surface"}]})[0])

    def test_newly_promoted_first_level_is_checked_again(self):
        original = tree([node("main"), node("shell", "main", [0, 0, 95, 95]),
            node("a", "shell", [0, 0, 40, 40]), node("b", "shell", [50, 50, 40, 40])])
        operations, _ = bottom_up_operations(original, {})
        result, _ = apply_operations(original, operations)
        self.assertEqual({n["node_id"] for n in result["nodes"]}, {"a", "b"})
        self.assertEqual(large_region_requirements(result, {}), [])

    def test_bottom_up_heading_content_group_preserves_frontier(self):
        original = tree([node("main"), node("shell", "main", [0, 0, 95, 95]),
            node("h1", "shell", [0, 0, 90, 10]), node("text1", "shell", [0, 12, 90, 20]),
            node("h2", "shell", [0, 50, 90, 10]), node("text2", "shell", [0, 62, 90, 20])])
        for n in original["nodes"][2:]:
            n["struct"] = "text"
            n["subtype"] = "heading" if n["node_id"].startswith("h") else "text"
        operations, evidence = bottom_up_operations(original, {})
        result, _ = apply_operations(original, operations)
        groups = [n for n in result["nodes"] if n.get("postprocess_generated")]
        self.assertEqual(len(groups), 2)
        self.assertEqual({n["node_id"] for n in result["nodes"] if not n.get("postprocess_generated")}, {"h1", "h2", "text1", "text2"})
        self.assertTrue(evidence)
        validate(result)

    def test_clipped_sliver_removed_but_useful_partial_kept(self):
        original = tree([node("tiny", box=[97, 0, 3, 80]), node("partial", box=[50, 0, 50, 80])])
        original["nodes"][0]["layout_bbox"] = [97, 0, 100, 80]
        original["nodes"][1]["layout_bbox"] = [50, 0, 100, 80]
        edits = clipped_fragment_operations(original, {})
        self.assertEqual([e["node"] for e in edits], ["tiny"])

    def test_data_table_not_automatically_treated_as_layout(self):
        original = tree([node("table")]+[node(f"row{i}", "table", [0, i*25, 100, 25]) for i in range(4)])
        original["nodes"][0]["struct"] = "table"
        for n in original["nodes"][1:]:
            n["subtype"] = "table_row"
        self.assertFalse(large_region_requirements(original, {})[0]["layout_table_candidate"])
        self.assertEqual(bottom_up_operations(original, {})[0], [])

    def test_model_cannot_scatter_promoted_layout_row(self):
        original = tree([node("row", box=[0, 0, 100, 30]), node("edge", "row", [0, 0, 2, 30]), node("nav", "row", [2, 0, 98, 30])])
        original["nodes"][0]["subtype"] = "table_row"
        result, applied, rejected = apply_model_edits(original, [{"op": "contract", "node": "row", "reason": "promote children"}])
        self.assertFalse(applied)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(next(n["parent_id"] for n in result["nodes"] if n["node_id"] == "edge"), "row")


class ApiTest(unittest.TestCase):
    def test_compatible_json_shapes_preserve_strict_verification(self):
        self.assertEqual(parse_response('[]')["operations"], [])
        self.assertEqual(parse_response('[{"op":"drop","node":"x","reason":"tiny"}]')["operations"][0]["node"], "x")
        self.assertTrue(parse_response('[{"accepted":true,"issues":[]}]', True)["accepted"])
        with self.assertRaises(ValueError):
            parse_response('[]', True)

    def test_mock_api_and_cli_isolation(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(payload)
                self.assert_data(payload)
                verify = "FINAL VERIFICATION" in payload["messages"][-1]["content"][0]["text"]
                answer = {"accepted": True, "issues": [], "large_region_decisions": [{"node_id": "p", "decision": "keep_single_component", "reason": "single visual component"}], "levels": [{"node_id": "p", "level": 1, "role": "main"}]} if verify else {
                    "operations": [], "issues": []}
                data = json.dumps({"model": "mock-vision", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 20}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)
            def assert_data(self, payload):
                assert self.path == "/v1/chat/completions"
                assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root/"input"
                source.mkdir()
                good = source/"good"
                good.mkdir()
                png(good/"full.png")
                file = good/"hierarchy.json"
                file.write_text(json.dumps(tree([{**node("p"), "struct": "image"}, node("a", "p")])))
                before = file.read_bytes()
                bad = source/"bad"
                bad.mkdir()
                (bad/"hierarchy.json").write_text("{}")
                config = {"api": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "mock-vision", "api_key": "test-secret"}}
                settings = root/"settings.json"
                settings.write_text(json.dumps(config))
                output = root/"output"
                execution = subprocess.run([sys.executable, str(Path(__file__).with_name("cli.py")),
                    "--input", str(source), "--output", str(output), "--config", str(settings)], capture_output=True, text=True)
                self.assertEqual(execution.returncode, 1)  # Partial due to bad page; good one still publishes.
                final = json.loads((output/"good/hierarchy.json").read_text())
                self.assertEqual(len(final["nodes"]), 1)
                self.assertEqual(file.read_bytes(), before)
                self.assertEqual(len(calls), 0)
                self.assertFalse((output/"bad/hierarchy.json").exists())
                self.assertNotIn("test-secret", (output/"good/layer-review.json").read_text())
                self.assertEqual(json.loads((output/"layer-postprocess-report.json").read_text())["counts"]["accepted"], 1)
                stamp = (output/"good/hierarchy.json").stat().st_mtime_ns
                resumed = subprocess.run([sys.executable, str(Path(__file__).with_name("cli.py")),
                    "--input", str(source), "--output", str(output), "--config", str(settings), "--resume"], capture_output=True, text=True)
                self.assertEqual(resumed.returncode, 1)  # Malformed input remains isolated.
                self.assertEqual(len(calls), 0)
                self.assertEqual((output/"good/hierarchy.json").stat().st_mtime_ns, stamp)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_failed_verification_does_not_publish(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/"input"
            source.mkdir()
            png(source/"full.png")
            file = source/"hierarchy.json"
            file.write_text(json.dumps(tree([node("p")])))
            with patch("legacy_pipeline.Client") as client:
                client.return_value.call.side_effect = [({"operations": []}, {}), ({"accepted": False, "issues": ["bad grain"]}, {})]
                record = process(file, source, root/"output", {"api": {"model": "mock"}}, False)
                self.assertEqual(record["status"], "needs_review")
                self.assertFalse((root/"output/hierarchy.json").exists())

    def test_accepted_boolean_cannot_publish_unresolved_large_root(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/"input"
            source.mkdir()
            png(source/"full.png")
            file = source/"hierarchy.json"
            file.write_text(json.dumps(tree([node("p")])))
            with patch("legacy_pipeline.Client") as client:
                client.return_value.call.side_effect = [({"operations": []}, {}), ({"accepted": True, "issues": []}, {})]
                record = process(file, source, root/"output", {"api": {"model": "mock"}}, False)
                self.assertEqual(record["status"], "needs_review")
                self.assertTrue(record["review_blockers"])
                self.assertFalse((root/"output/hierarchy.json").exists())

    def test_old_normalization_cache_not_replayed(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/"input"
            source.mkdir()
            png(source/"full.png")
            file = source/"hierarchy.json"
            file.write_text(json.dumps(tree([node("p")])))
            cache = root/"old.json"
            cache.write_text(json.dumps({"source": str(file), "postprocess_version": "visual_layers_vlm_v3", "edit_response": {"operations": []}}))
            with patch("legacy_pipeline.Client") as client:
                record = process(file, source, root/"output", {"api": {"model": "mock"}}, False, cache)
                self.assertEqual(record["status"], "failed")
                self.assertIn("older normalization", record["error"])
                client.assert_not_called()

    def test_reuse_paid_edits_only_calls_verification(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/"input"
            source.mkdir()
            png(source/"full.png")
            file = source/"hierarchy.json"
            file.write_text(json.dumps(tree([node("p"), node("a", "p")])))
            old = root/"old.json"
            old.write_text(json.dumps({"source": str(file), "postprocess_version": PIPELINE_VERSION, "edit_response": {"operations": [
                {"op": "contract", "node": "a", "reason": "old paid edit"}]}}))
            with patch("legacy_pipeline.Client") as client:
                client.return_value.call.return_value = ({"accepted": True, "issues": [], "large_region_decisions": [
                    {"node_id": "p", "decision": "keep_single_component", "reason": "single surface"}]}, {})
                record = process(file, source, root/"output", {"api": {"model": "mock"}}, False, old)
                self.assertEqual(record["status"], "accepted")
                self.assertEqual(client.return_value.call.call_count, 1)
                self.assertTrue(client.return_value.call.call_args.kwargs["verify"])
                repeated = process(file, source, root/"output2", {"api": {"model": "mock"}}, False, root/"output/layer-review.json")
                self.assertEqual(repeated["status"], "accepted")
                self.assertEqual(client.return_value.call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
