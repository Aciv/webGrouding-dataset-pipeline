"""Deterministic restricted grouping, request and default pipeline regressions; no paid API."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from grouping import candidates, apply_decisions, prune_depth
from grouping_vlm import Client
from grouping_pipeline import process
from rules import validate
from test_postprocess import node, tree, png


def fixture():
    return tree([node('p', box=[0, 0, 95, 55]),
        {**node('a', 'p', [5, 5, 40, 10]), 'text_role': 'heading'},
        node('b', 'p', [5, 20, 40, 20]), node('c', 'p', [65, 5, 15, 10])])


def accept(g):
    return {'candidate_id': g['candidate_id'], 'decision': 'accept',
        'members': g['members'], 'region_type': 'heading_content'}


class GroupingTest(unittest.TestCase):
    def test_candidates_reach_deeper_families_and_are_bounded(self):
        original = fixture()
        original['nodes'] += [node('d', 'b', [5, 20, 15, 5]), node('e', 'b', [5, 30, 15, 5])]
        groups = candidates(original, {'max_candidates': 5})
        self.assertLessEqual(len(groups), 5)
        self.assertTrue(any(g['parent_id'] == 'b' for g in groups))
        self.assertEqual(groups, candidates(original, {'max_candidates': 5}))

    def test_new_parent_preserves_all_original_nodes_and_pixels(self):
        original = fixture()
        frozen = deepcopy(original)
        proposed = candidates(original)
        result, ops, rejected, unresolved = apply_decisions(original, proposed,
            {'groups': [accept(proposed[0])]+[{'candidate_id': g['candidate_id'], 'decision': 'reject'} for g in proposed[1:]]})
        self.assertEqual(original, frozen)
        self.assertEqual(len(ops), 1)
        self.assertEqual(rejected, [])
        self.assertEqual(unresolved, [])
        actual = {n['node_id']: n for n in result['nodes']}
        for n in original['nodes']:
            self.assertEqual(actual[n['node_id']]['bbox'], n['bbox'])
        self.assertEqual(len(result['nodes']), len(original['nodes'])+1)
        validate(result, 4)

    def test_operations_and_invented_members_are_forbidden(self):
        original = fixture(); proposed = candidates(original)
        with self.assertRaises(ValueError):
            apply_decisions(original, proposed, {'operations': [{'op': 'drop', 'node': 'a'}]})
        choice = accept(proposed[0]); choice['members'] = ['a', 'invented']
        result, applied, rejected, unresolved = apply_decisions(original, proposed, {'groups': [choice]})
        self.assertFalse(applied); self.assertTrue(rejected); self.assertTrue(unresolved)
        self.assertEqual(result, original)

    def test_depth_budget_excludes_overflow(self):
        original = fixture()
        original['nodes'] += [node('d', 'b', [5, 20, 15, 5]), node('e', 'd', [5, 20, 5, 5])]
        self.assertFalse(any('b' in g['members'] for g in candidates(original)))

    def test_overlapping_decisions_missing_uncertain_and_bad_types(self):
        original = fixture(); proposed = candidates(original)
        proposed.append({**proposed[0], 'candidate_id': 'duplicate_geometry'})
        _, applied, rejected, unresolved = apply_decisions(original, proposed, {'groups': [accept(proposed[0]), accept(proposed[-1])]})
        self.assertEqual(len(applied), 1); self.assertTrue(rejected); self.assertTrue(unresolved)
        _, _, _, unresolved = apply_decisions(original, proposed, {'groups': []})
        self.assertEqual(len(unresolved), len(proposed))
        bad = accept(proposed[0]); bad['region_type'] = 'invented'
        updated, _, errors, _ = apply_decisions(original, proposed, {'groups': [bad]})
        self.assertFalse(errors)
        self.assertTrue(any(n.get('region_type') == 'unknown' for n in updated['nodes']))

    def test_subset_and_repeated_synthetic_id_remain_valid(self):
        original = fixture()
        proposed = [{'candidate_id': 'g001', 'parent_id': 'p', 'members': ['a', 'b', 'c']}]
        original['postprocess_sources'] = {'vlm_group_g001': deepcopy(original['nodes'][0])}
        choice = accept(proposed[0]); choice['members'] = ['a', 'b']
        result, applied, rejected, unresolved = apply_decisions(original, proposed, {'groups': [choice]})
        self.assertFalse(rejected); self.assertFalse(unresolved)
        self.assertEqual(applied[0]['id'], 'vlm_group_g001_new')
        self.assertEqual(next(n for n in result['nodes'] if n['node_id'] == 'c')['parent_id'], 'p')

    def test_nested_groups_omit_only_beyond_four_with_provenance(self):
        original = tree([node('p', box=[0, 0, 95, 95]),
            node('a', 'p', [5, 5, 40, 40]), node('b', 'p', [55, 5, 20, 20]),
            node('c', 'a', [5, 5, 10, 10]), node('d', 'a', [5, 20, 10, 10])])
        proposed = [{'candidate_id': 'top', 'members': ['a', 'b']},
            {'candidate_id': 'inside', 'members': ['c', 'd']}]
        result, ops, rejected, _ = apply_decisions(original, proposed,
            {'groups': [accept(g) for g in proposed]}, overflow_policy='drop')
        self.assertFalse(rejected)
        self.assertEqual({op['node'] for op in ops if op['op'] == 'drop'}, {'c', 'd'})
        self.assertEqual({op['omitted_level'] for op in ops if op['op'] == 'drop'}, {5})
        self.assertEqual({r['node_id'] for r in result['ignore_regions']}, {'c', 'd'})
        self.assertTrue({'c', 'd'} <= set(result['postprocess_sources']))
        self.assertTrue({'p', 'a', 'b'} <= {n['node_id'] for n in result['nodes']})
        validate(result, 4)

    def test_depth_pruning_is_idempotent(self):
        original = fixture()
        result, ops = prune_depth(original)
        self.assertFalse(ops)
        self.assertEqual(result, original)

    def test_bad_candidate_limit_is_rejected(self):
        for limit in (0, -1, True, 257):
            with self.assertRaises(ValueError):
                candidates(fixture(), {'max_candidates': limit})

    def test_request_has_original_evidence_no_edit_history(self):
        original = fixture()
        client = Client({'api_key': 'test', 'model': 'mock', 'base_url': 'http://localhost:9999'}, original)
        messages, stats = client.messages(original, 'data:image/png;base64,test', {'candidates': candidates(original)}, client.task())
        value = json.loads(messages[1]['content'][0]['text'].split('\n', 1)[1])
        self.assertIn('json', messages[0]['content'].lower())
        self.assertEqual(set(value), {'original', 'current_tree', 'candidates', 'pixel_candidates'})
        self.assertEqual(stats['original_box_rows'], len(original['nodes']))
        self.assertEqual(sum(c['type'] == 'image_url' for c in messages[1]['content']), 1)
        with self.assertRaises(ValueError):
            client.messages(original, '', {}, '', verify=True)

    def test_pipeline_single_call_and_no_full_tree_verified_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root/'source'; source.mkdir()
            file = source/'hierarchy.json'; file.write_text(json.dumps(fixture())); png(source/'full.png')
            frozen = file.read_bytes()
            def reply(tree, image, context):
                return {'groups': [accept(context['candidates'][0])]+[{'candidate_id': g['candidate_id'], 'decision': 'reject'} for g in context['candidates'][1:]]}, {'usage': {'prompt_tokens': 123, 'completion_tokens': 20}}
            with patch('grouping_pipeline.Client') as api:
                api.return_value.call.side_effect = reply
                record = process(file, source, root/'out', {'api': {'model': 'mock'}, 'accumulation': {'fold_sparse_min_children': 2}}, False)
                self.assertEqual(record['status'], 'accepted')
                self.assertEqual(api.return_value.call.call_count, 1)
                result = json.loads((root/'out/hierarchy.json').read_text())
                self.assertFalse(result['postprocess']['whole_tree_vlm_verified'])
                self.assertEqual(sum(op['op'] == 'group' for op in result['postprocess']['operations'] if op['reason'].startswith('vlm_candidate_')), 1)
                self.assertEqual(file.read_bytes(), frozen)
                cached = process(file, source, root/'out2', {'api': {'model': 'mock'}, 'accumulation': {'fold_sparse_min_children': 2}}, False, root/'out/layer-review.json')
                self.assertEqual(cached['status'], 'accepted')
                self.assertEqual(api.return_value.call.call_count, 1)

    def test_unselected_candidates_preserve_rule_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root/'source'; source.mkdir()
            file = source/'hierarchy.json'; file.write_text(json.dumps(fixture())); png(source/'full.png')
            with patch('grouping_pipeline.Client') as api:
                api.return_value.call.return_value = ({'groups': []}, {})
                record = process(file, source, root/'out', {'api': {'model': 'mock'}, 'accumulation': {'fold_sparse_min_children': 2}}, False)
            self.assertEqual(record['status'], 'accepted')
            audit = json.loads((root/'out/layer-review.json').read_text())
            self.assertTrue(audit['grouping_warnings'])
            self.assertEqual(json.loads((root/'out/rule-hierarchy.json').read_text())['nodes'],
                json.loads((root/'out/hierarchy.json').read_text())['nodes'])

    def test_no_candidates_skip_api_and_large_atomic_is_exempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); file = root/'hierarchy.json'
            file.write_text(json.dumps(tree([{**node('img'), 'struct': 'image'}]))); png(root/'full.png')
            with patch('grouping_pipeline.Client') as api:
                record = process(file, root, root/'out', {'api': {'model': 'mock'}, 'accumulation': {'fold_sparse_min_children': 2}}, False)
                self.assertEqual(record['status'], 'accepted'); api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
