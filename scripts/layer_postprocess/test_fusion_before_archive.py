"""Exercise real normalization/grouping with mocked model transport, never a paid API."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

from grouping_pipeline import process
from grouping import candidates, apply_decisions
from rules import validate
from test_postprocess import node, tree


def full_tree():
    """A coarse L4 wrapper contains two coherent L5 pieces and one distant residual."""
    result = tree([
        {**node('root', box=[0, 0, 900, 900]), 'struct': 'region'},
        {**node('l2', 'root', [0, 0, 800, 800]), 'struct': 'region'},
        {**node('l3', 'l2', [0, 0, 700, 700]), 'struct': 'region'},
        node('l4', 'l3', [0, 0, 600, 600]),
        {**node('title', 'l4', [10, 10, 250, 40]), 'struct': 'text', 'text_role': 'heading'},
        {**node('body', 'l4', [10, 65, 250, 150]), 'struct': 'image'},
        {**node('rest', 'l4', [400, 300, 100, 100]), 'struct': 'control'},
    ])
    result['capture'].update(width=1000, height=1000)
    return result


class FusionBeforeArchiveTest(unittest.TestCase):
    def run_case(self, root, reply, *, dry_run=False):
        """Run the public processing entry, retaining real geometry and storage code."""
        source = root/'source'; source.mkdir()
        file = source/'hierarchy.json'; file.write_text(json.dumps(full_tree()))
        Image.new('RGB', (1000, 1000), 'white').save(source/'full.png')
        before = file.read_bytes()
        config = {'api': {'model': 'mock'}, 'rules': {'layer1_max_area_ratio': .95},
            'accumulation': {'fold_sparse_min_children': 2},
            # Legacy drop must not prune deep fusion evidence before final archival.
            'grouping': {'depth_overflow_policy': 'drop'}}
        with patch('grouping_pipeline.Client') as api:
            api.return_value.call.side_effect = reply
            record = process(file, source, root/'out', config, dry_run)
            if dry_run: api.assert_not_called()
            else: self.assertEqual(api.return_value.call.call_count, 1)
        self.assertEqual(file.read_bytes(), before)
        return record, json.loads((root/'out/candidate-hierarchy.json').read_text()), json.loads((root/'out/layer-review.json').read_text())

    def test_l5_members_repair_l4_before_archival(self):
        def reply(full, image, context):
            self.assertFalse(full.get('detail_archive'))
            depths = validate(full)
            self.assertEqual(depths['title'], 4)
            candidate = next(g for g in context['candidates'] if set(g['members']) == {'title', 'body'})
            self.assertEqual(candidate['replacement_plan']['upper_level'], 4)
            return {'groups': [{'candidate_id': candidate['candidate_id'], 'decision': 'accept',
                'region_type': 'image_with_caption', 'members': candidate['members']}]}, {}
        with tempfile.TemporaryDirectory() as tmp:
            record, result, audit = self.run_case(Path(tmp), reply)
        self.assertEqual(record['status'], 'accepted')
        depths = validate(result, 4)
        group = next(n for n in result['nodes'] if n.get('region_type') == 'image_with_caption')
        self.assertEqual(depths[group['node_id']], 3)
        self.assertEqual(group['bbox'], [10, 10, 250, 205])
        # Established unary cleanup may reuse the residual control itself as L4.
        self.assertEqual(next(n['bbox'] for n in result['nodes'] if n['node_id'] == 'rest'), [400, 300, 100, 100])
        archive = {n['node_id']: n for n in result['detail_archive']['nodes']}
        self.assertTrue({'title', 'body'} <= archive.keys())
        self.assertEqual(archive['title']['owner_id'], group['node_id'])
        self.assertEqual(archive['body']['bbox'], [10, 65, 250, 150])
        self.assertFalse(audit['depth_pruning_operations'])
        self.assertEqual(audit['pre_archive_fusion']['applied_replacements'], 1)

    def test_rejection_keeps_l4_and_archives_deep_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, result, audit = self.run_case(Path(tmp), lambda *args: ({'groups': []}, {}))
        self.assertEqual(record['status'], 'accepted')
        self.assertEqual(next(n['bbox'] for n in result['nodes'] if n['node_id'] == 'l4'), [0, 0, 600, 600])
        self.assertEqual(len(result['detail_archive']['nodes']), 3)
        self.assertFalse(audit['applied_upper_replacements'])
        validate(result, 4)

    def test_dry_preview_preserves_separate_full_fusion_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record, result, audit = self.run_case(root, None, dry_run=True)
            fusion = json.loads((root/'out/fusion-input-hierarchy.json').read_text())
        self.assertEqual(record['status'], 'proposed')
        self.assertEqual(max(validate(fusion).values()), 4)
        validate(result, 4)
        self.assertEqual(audit['deep_group_candidates'], 1)

    def test_provider_shallow_first_order_executes_deep_repair_first(self):
        original = full_tree()
        original['nodes'] += [node('other', box=[0, 910, 900, 90]),
            {**node('sa', 'other', [10, 915, 100, 10]), 'text_role': 'heading'},
            {**node('sb', 'other', [10, 930, 100, 20]), 'struct': 'image'},
            node('sc', 'other', [700, 915, 100, 20])]
        policy = {'group_action': 'replace_upper', 'prefer_existing_region_replacement': True,
            'conservative_grouping': True, 'preserve_meaningful_deep': True}
        proposed = candidates(original, policy)
        deep = next(g for g in proposed if set(g['members']) == {'title', 'body'})
        shallow = next(g for g in proposed if set(g['members']) == {'sa', 'sb'})
        response = {'groups': [{'candidate_id': g['candidate_id'], 'decision': 'accept',
            'members': g['members'], 'region_type': 'heading_content'} for g in (shallow, deep)]}
        result, ops, rejected, _ = apply_decisions(original, proposed, response, semantic_policy=policy)
        self.assertFalse(rejected)
        self.assertEqual([set(op['nodes']) for op in ops if op['op'] == 'group'],
            [{'title', 'body'}, {'sa', 'sb'}])
        validate(result)


if __name__ == '__main__':
    unittest.main()
