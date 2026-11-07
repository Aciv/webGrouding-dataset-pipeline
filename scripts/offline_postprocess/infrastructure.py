"""Atomic local persistence and bounded source discovery; source files are always read-only."""
import hashlib
import json
import os
from pathlib import Path


def sha256_file(path):
    """Hash source/artifact bytes in bounded blocks without decoding or modifying them."""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    """Atomically publish JSON after successful serialization; temporary file is same-directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(payload+'\n', encoding='utf-8')
    os.replace(temporary, path)


def write_jsonl(path, records):
    """Stream deterministic JSONL to a same-directory temporary file, then atomically publish."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n')
    os.replace(temporary, path)


def read_jsonl(path):
    """Yield one JSON record at a time; close the file when iteration finishes or generator closes."""
    with open(path, encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def discover(root):
    """Discover page/viewport hierarchy pairs, assigning identity within this source dataset only."""
    root = Path(root).resolve()
    pages = root/'pages' if (root/'pages').is_dir() else root
    items = []
    for path in sorted(pages.glob('*/*/hierarchy.json')):
        page_id, viewport = path.parent.parent.name, path.parent.name
        items.append({'page_id': page_id, 'viewport': viewport,
                      'observation_id': f'{page_id}/{viewport}', 'hierarchy': str(path),
                      'image': str(path.with_name('full.png'))})
    if not items:
        raise ValueError(f'No page/viewport/hierarchy.json under {pages}')
    return items
