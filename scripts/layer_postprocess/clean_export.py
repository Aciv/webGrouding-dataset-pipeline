"""Export only canonical PNG + legacy-schema hierarchy, truncated at real parent depth L5."""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import shutil

from rules import validate
from storage import image_path, write


NODE_FIELDS = (
    'struct', 'subtype', 'kind', 'disposition', 'semantic_role', 'text_role',
    'source_node_ids', 'structure_reasons', 'node_id', 'parent_id', 'layer',
    'semantic_hint', 'tag', 'selector', 'bbox', 'source_dom_parent_id',
    'layout_bbox', 'interaction_role', 'fragments', 'geometry_warnings',
    'reasons', 'selection_priority',
)


def clean_hierarchy(fused):
    """Project an already cleaned/fused tree to five true levels, without promoting archive boxes.

    Coordinates stay in screenshot CSS pixels. Parent edges are retained, roots use
    layer=0, L5 uses layer=4; deeper descendants are truncated, never flattened.
    """
    depths = validate(fused)
    nodes = []
    for source in fused['nodes']:
        if depths[source['node_id']] >= 5:
            continue
        value = {key: deepcopy(source[key]) for key in NODE_FIELDS if key in source}
        value['layer'] = depths[source['node_id']]
        # Synthetic approved regions still use the original VisualNode shape.
        value.setdefault('semantic_hint', source.get('region_type', source.get('subtype', 'visual_group')))
        value.setdefault('tag', '')
        value.setdefault('selector', '')
        value.setdefault('source_dom_parent_id', None)
        value.setdefault('layout_bbox', list(value['bbox']))
        value.setdefault('interaction_role', None)
        value.setdefault('fragments', [])
        value.setdefault('geometry_warnings', [])
        value.setdefault('reasons', [])
        nodes.append(value)
    result = {key: deepcopy(fused[key]) for key in (
        'structure_rule_version', 'capture', 'geometry_policy') if key in fused}
    result.update(schema='visual_hierarchy_candidate_v1',
        rule_version='visual_layers_fusion_clean5_v13',
        annotation_status='rule_generated_unverified',
        parent_basis='retained_dom_ancestor_with_sibling_absorption',
        nodes=nodes, decisions=[], ignore_regions=deepcopy(fused.get('ignore_regions', [])))
    stats = fused.get('stats', {})
    result['stats'] = {key: stats.get(key, 0) for key in (
        'raw_visible', 'visible_after_clipping', 'ancestor_clipped_out',
        'ancestor_clipped_partial', 'area_filtered_out', 'css_hidden_out', 'screen_reader_only_out')}
    result['stats'].update(retained=len(nodes),
        excluded=max(0, result['stats']['raw_visible']-len(nodes)),
        roots=sum(n['parent_id'] is None for n in nodes),
        max_layer=max((n['layer'] for n in nodes), default=-1))
    for field in ('layer', 'kind', 'struct', 'disposition'):
        result['stats']['by_'+field] = dict(Counter(str(n.get(field)) for n in nodes))
    validate(result, 5)
    return result


def export_clean(source, output):
    """Write only image/label pairs under output/pages; keep processing evidence outside export.

    Refuse failed/unreviewed runs and path collisions before writing any pair.
    Source may be a processing run root, and output must be separate and empty.
    """
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError('Processing run does not exist')
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Export must be separate from processing input')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Export output must be empty; no overwriting')
    jobs = []
    for file in sorted(source.rglob('fused-hierarchy.json')):
        if file.is_symlink():
            raise ValueError('Symlink fusion input is unsupported')
        audit = json.loads(file.with_name('layer-review.json').read_text())
        if audit['status'] != 'accepted':
            raise ValueError('Not structurally accepted: '+str(file.parent))
        fused = json.loads(file.read_text())
        clean = clean_hierarchy(fused)
        image = image_path(file, fused)
        relative = file.parent.relative_to(source)
        destination = output/'pages'/relative
        jobs.append((image, clean, destination))
    report = json.loads((source/'layer-postprocess-report.json').read_text())
    if not jobs or len(jobs) != len(report['observations']):
        raise ValueError('Missing accepted fused trees; finish/retry processing before clean export')
    for image, clean, destination in jobs:
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(image, destination/image.name)
        write(destination/'hierarchy.json', clean)
    return {'observations': len(jobs), 'files': len(jobs)*2,
        'nodes': sum(len(clean['nodes']) for _,clean,_ in jobs),
        'by_layer': dict(sorted(sum((Counter(clean['stats']['by_layer']) for _,clean,_ in jobs), Counter()).items())),
        'output': str(output/'pages')}


def main():
    """Export an offline processed run without model calls or source modification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(export_clean(args.input, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
