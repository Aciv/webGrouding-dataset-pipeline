"""Small local layer fixtures keep original/current depth and crop repetitions distinct."""
import tempfile
import unittest
from pathlib import Path
from scripts.offline_postprocess.infrastructure import write_json, write_jsonl, sha256_file
from scripts.offline_postprocess.layer_statistics import compute_layer_statistics


class LayerStatisticsTests(unittest.TestCase):
    def test_inactive_ancestors_context_duplicates_and_crop_only_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nodes = [
                {'node_id':'root','global_node_id':'p/mobile/root','parent_id':None,'derived_depth':0,
                 'source_layer':0,'detection_eligible':False,'source_detection_eligible':False},
                {'node_id':'a','global_node_id':'p/mobile/a','parent_id':'root','derived_depth':1,
                 'source_layer':7,'detection_eligible':True,'source_detection_eligible':True,'struct':'text'},
                {'node_id':'b','global_node_id':'p/mobile/b','parent_id':'a','derived_depth':2,
                 'source_layer':None,'detection_eligible':True,'source_detection_eligible':True,'struct':'image'}]
            graph = root/'graph.json';write_json(graph,{'nodes':nodes})
            manifest = root/'crops.jsonl'
            write_jsonl(manifest,[{'targets':[{'global_node_id':'p/mobile/a'}],'detail_training_eligible':True},
                                  {'targets':[{'global_node_id':'p/mobile/a'},{'global_node_id':'p/mobile/b'}],
                                   'detail_training_eligible':False},
                                  {'targets':[{'global_node_id':'p/mobile/a'}],'detail_training_eligible':True}])
            index = root/'index.json'
            write_json(index,{'schema':'gui_component_dataset_index_v1','publication_status':'validated',
                'rule_version':'fixture','observations':[{'status':'accepted','observation_id':'p/mobile',
                    'hierarchy':str(graph),'hierarchy_sha256':sha256_file(graph)}],
                'artifacts':{'detection_crops':{'path':str(manifest),'sha256':sha256_file(manifest)}}})
            result = compute_layer_statistics(index,workers=1)
            layers = {r['layer']:r for r in result['layers']}
            self.assertEqual(layers['L1']['global_detection_targets'],0)
            self.assertEqual(layers['L2']['detail_training_unique_targets'],1)
            self.assertEqual(layers['L2']['detail_training_occurrences'],2)
            self.assertEqual(layers['L2']['withheld_only_by_detail_selection'],0)
            self.assertEqual(layers['L3']['withheld_only_by_detail_selection'],1)
            self.assertEqual(result['totals']['candidate_crop_occurrences'],4)
            self.assertEqual(result['totals']['candidate_unique_targets'],2)
            self.assertEqual(result['totals']['detail_discarded_occurrences'],2)
            original = {r['source_layer']:r for r in result['source_layers']}
            self.assertEqual(original['7']['source_detection_candidates'],1)
            self.assertEqual(original['unknown']['source_detection_candidates'],1)
            self.assertTrue((root/'reports/layer-statistics.csv').exists())


if __name__ == '__main__':
    unittest.main()
