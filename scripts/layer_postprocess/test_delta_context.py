"""Loss checks for compact request contexts and original-first conversation handling."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from delta_context import build_delta, original_boxes, reconstruct_boxes, row, current_tree, reconstruct_current_boxes, review_patch, reconstruct_review_boxes
from rules import apply_operations
from vlm import Client
from test_postprocess import node, tree


class DeltaContextTest(unittest.TestCase):
    def test_patch_reconstructs_contract_group_and_reparent(self):
        before = tree([node("root"), node("wrap", "root", [0, 0, 80, 80]), node("a", "wrap", [0, 0, 20, 20]),
            node("b", "wrap", [30, 0, 20, 20]), node("other", "root", [0, 90, 20, 10])])
        before, _ = apply_operations(before, [])
        ops = [{"op": "contract", "node": "wrap", "reason": "wrapper"},
            {"op": "group", "nodes": ["a", "b"], "id": "vlm_group_1", "reason": "same component"},
            {"op": "reparent", "node": "other", "parent": "vlm_group_1", "reason": "belongs here"}]
        after, _ = apply_operations(before, ops)
        frozen = deepcopy(before)
        delta = build_delta(before, after, ops)
        self.assertEqual(reconstruct_boxes(before, delta), {n["node_id"]: row(n) for n in after["nodes"]})
        self.assertEqual(before, frozen)
        sparse = review_patch(before, after, ops)
        self.assertEqual(reconstruct_review_boxes(before, sparse), {n["node_id"]: row(n) for n in after["nodes"]})
        self.assertEqual(sparse["before"]["a"], ["wrap", 2])
        self.assertEqual(set(original_boxes(before)["boxes"]), {n["node_id"] for n in before["nodes"]})

    def test_layer_only_descendants_not_enumerated(self):
        before = tree([node("root"), node("wrap", "root", [0, 0, 90, 90]), node("a", "wrap", [0, 0, 80, 80])]+
            [node("leaf"+str(i), "a", [i, 0, 1, 1]) for i in range(50)])
        before, _ = apply_operations(before, [])
        ops = [{"op": "contract", "node": "wrap", "reason": "long explanation that should not be transmitted"}]
        after, _ = apply_operations(before, ops)
        delta = build_delta(before, after, ops)
        self.assertLess(len(delta["after"]), 10)
        self.assertTrue(delta["subtree_layer_shifts"])
        self.assertNotIn("long explanation", json.dumps(delta))
        self.assertEqual(reconstruct_boxes(before, delta), {n["node_id"]: row(n) for n in after["nodes"]})

    def test_second_turn_appends_delta_without_new_image(self):
        original, _ = apply_operations(tree([node("p"), node("a", "p", [0, 0, 20, 20])]), [])
        current, _ = apply_operations(original, [{"op": "contract", "node": "p", "reason": "wrapper"}])
        client = Client({"base_url": "http://localhost:1/v1", "model": "mock", "api_key": "fake"}, original_tree=original)
        first, _ = client.messages(current, "data:image/png;base64,AA==", {"applied_rule_edits": []}, "EDIT")
        initial = json.loads(first[1]["content"][0]["text"].split("\n", 1)[1])
        proposal = json.loads(first[2]["content"][0]["text"].split("\n", 1)[1])
        self.assertEqual(set(initial["original"]["boxes"]), {"p", "a"})
        self.assertEqual(proposal["current_tree"]["roots"], ["a"])
        self.assertEqual(initial["original"]["box_columns"], ["class", "bbox", "active_in_initial_candidate"])
        self.assertEqual(initial["original"]["boxes"]["p"][-1], 0)
        self.assertEqual(initial["original"]["boxes"]["a"][-1], 1)
        self.assertEqual(reconstruct_current_boxes(original, proposal["current_tree"]), {n["node_id"]: row(n) for n in current["nodes"]})
        client.history = first+[{"role": "assistant", "content": '{"operations":[]}'}]
        client.previous_tree = current
        second, stats = client.messages(current, "data:image/png;base64,AA==", {"review_operations": []}, "VERIFY", True)
        self.assertEqual(second[1], client.history[1])
        self.assertEqual(second[2], client.history[-1])
        self.assertNotIn(first[2], second)
        self.assertIn("CURRENT PHASE IS FINAL VERIFICATION", second[0]["content"])
        self.assertTrue(all(c["type"] == "text" for c in second[-1]["content"]))
        self.assertEqual(stats["new_image_blocks"], 0)
        self.assertEqual(sum(c.get("type") == "image_url" for m in second if isinstance(m["content"], list) for c in m["content"]), 1)

    def test_legacy_mode_remains_available(self):
        sample, _ = apply_operations(tree([node("a")]), [])
        client = Client({"base_url": "http://localhost:1/v1", "model": "mock", "api_key": "fake", "context_mode": "full"})
        messages, _ = client.messages(sample, "data:image/png;base64,AA==", {}, "EDIT")
        value = json.loads(messages[1]["content"][0]["text"].split("\n", 1)[1])
        self.assertIn("tree", value)
        self.assertEqual(len(value["tree"]["nodes"]), 1)


if __name__ == "__main__":
    unittest.main()
