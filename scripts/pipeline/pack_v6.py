"""Pack V6 core-crop exports into deterministic tar.gz shards with per-crop metadata."""
from collections import defaultdict
import hashlib
import json
import math
import tarfile
from pathlib import Path

from scripts.offline_postprocess.infrastructure import sha256_file, write_json

SCHEMA = 'gui_component_crop_pack_v6'


def pack_v6_export(root, output, chunk_size_mb=1024, resume=False):
    """Compress `pages/<viewport>/*.png|.json` into page-grouped shards.

    Each shard keeps the V6 relative member paths. `metadata.jsonl` has one row per crop
    with URL, member paths, hashes and V5 detail flags. The source export is never modified.
    """
    root, output = Path(root).resolve(), Path(output).resolve()
    if not math.isfinite(chunk_size_mb) or chunk_size_mb <= 0:
        raise ValueError('chunk_size_mb must be positive')
    manifest = json.loads((root/'manifest.json').read_text())
    if manifest.get('contract') != 'gui_component_crop_export_v6':
        raise ValueError('A V6 core-crop export is required')
    source_hash = sha256_file(root/'manifest.json')
    marker = output/'manifest.json'
    if output.exists() and any(output.iterdir()):
        if resume and marker.is_file():
            previous = json.loads(marker.read_text())
            if (previous.get('status') == 'completed' and previous.get('source_manifest_sha256') == source_hash
                    and previous.get('chunk_size_mb') == chunk_size_mb):
                return {**previous, 'resumed': True, 'output': str(output)}
        raise ValueError('V6 pack output must be empty or matching --resume output')
    output.mkdir(parents=True, exist_ok=True)
    (output/'data').mkdir(exist_ok=True)
    records = []
    for index, crop in enumerate(json.loads((root/'crops-index.json').read_text())['crops']):
        image, label = root/crop['image'], root/crop['label']
        if sha256_file(image) != crop['image_sha256'] or sha256_file(label) != crop['label_sha256']:
            raise ValueError('V6 source crop changed: '+crop['crop_id'])
        value = json.loads(label.read_text())
        records.append({**crop, 'index': index, 'url': value.get('url'), 'title': value.get('title'),
                        'target_count': value.get('target_count'), 'split_supervision_eligible': value.get('split_supervision_eligible'),
                        'image_bytes': image.stat().st_size, 'label_bytes': label.stat().st_size})
    pages = defaultdict(list)
    for record in records:
        pages[record['page_id']].append(record)
    groups, current, current_bytes = [], [], 0
    limit = int(chunk_size_mb*1024*1024)
    for page_id in sorted(pages):
        group = pages[page_id]
        size = sum(r['image_bytes']+r['label_bytes'] for r in group)
        if current and current_bytes+size > limit:
            groups.append(current); current, current_bytes = [], 0
        current.extend(group); current_bytes += size
    if current:
        groups.append(current)
    shards, rows = [], []
    for number, group in enumerate(groups):
        name = f'data/train-{number:05d}.tar.gz'
        path = output/name
        with tarfile.open(path, 'w:gz', compresslevel=6) as archive:
            for record in group:
                for key in ('image', 'label'):
                    member = root/record[key]
                    archive.add(member, arcname=record[key], recursive=False)
                rows.append({
                    'index': record['index'], 'page_id': record['page_id'], 'viewport': record['viewport'],
                    'crop_id': record['crop_id'], 'crop_order': record['crop_order'],
                    'url': record.get('url'), 'title': record.get('title'),
                    'chunk': name, 'image_member': record['image'], 'label_member': record['label'],
                    'image_sha256': record['image_sha256'], 'label_sha256': record['label_sha256'],
                    'target_count': record.get('target_count'),
                    'detail_training_eligible': record.get('detail_training_eligible'),
                    'detail_training_discarded': record.get('detail_training_discarded'),
                    'split_supervision_eligible': record.get('split_supervision_eligible'),
                })
        shards.append({'file': name, 'crops': len(group), 'bytes': path.stat().st_size,
                       'sha256': sha256_file(path)})
    rows.sort(key=lambda row: row['index'])
    with (output/'metadata.jsonl').open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'))+'\n')
    summary = {
        'schema': SCHEMA, 'status': 'completed', 'layout': 'data/train-NNNNN.tar.gz',
        'core_crop': True, 'chunk_size_mb': chunk_size_mb, 'crops': len(records),
        'pages': len(pages), 'shards': shards,
        'source_manifest_sha256': source_hash, 'source_export': str(root),
        'members': 'PNG and same-stem JSON at their V6 pages/<viewport>/ paths',
    }
    write_json(marker, summary)
    (output/'README.md').write_text(
        '# V6 core-crop package\n\n'
        'Each shard contains V6 core-height PNG crops and same-stem JSON labels at their '
        '`pages/<viewport>/<page_id>_<crop_order>` paths. `metadata.jsonl` indexes every '
        'crop with its URL, members, hashes and V5 detail-training flags. Labels are '
        'core-crop-local; context `crop_bbox` is intentionally absent. Verify shard SHA-256 '
        'against `manifest.json` before use. This package is independent of the V1 pack format.\n', encoding='utf-8')
    return {**summary, 'output': str(output)}
