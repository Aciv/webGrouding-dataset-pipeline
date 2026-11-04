#!/usr/bin/env python3
"""Sample existing human-assigned Curlie categories; no model-based relabeling.

Read the compressed directory twice without extracting files or loading all sites.
Keep bounded per-category reservoirs, then interleave categories and deduplicate
exact hostnames (www normalized). Directory membership is not live-site validation.
"""
import argparse
import collections
import json
import random
from pathlib import Path
from urllib.parse import urlsplit
from curlie.archive import rows


def write_json(file, value):
    """Write readable UTF-8 metadata and compatible collector configuration."""
    file.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    """Build a deterministic categorized candidate set and existing-runner shards."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--output", default="examples/curlie-categorized")
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--categories", nargs="+", default=[
        "Arts", "Business", "Computers", "Games", "Health", "Home", "News",
        "Recreation", "Reference", "Science", "Shopping", "Society", "Sports"])
    flags = parser.parse_args()
    if flags.count < 1 or flags.concurrency < 1:
        parser.error("count and concurrency must be positive")
    output = Path(flags.output)
    output.mkdir(parents=True, exist_ok=True)
    categories = {}
    for row in rows(flags.archive, "-s.tsv"):
        if len(row) >= 2:
            label = row[1].removeprefix("Top/")
            if label.split("/")[0] in flags.categories:
                categories[row[0]] = label
    rng = random.Random(flags.seed)
    counts = collections.Counter()
    pools = {category: [] for category in flags.categories}
    for row in rows(flags.archive, "-c.tsv"):
        if len(row) < 4 or row[3] not in categories:
            continue
        label = categories[row[3]]
        category = label.split("/")[0]
        try:
            url = urlsplit(row[0])
            if url.scheme not in ("http", "https") or not url.hostname or url.username:
                continue
            host = url.hostname.lower().removeprefix("www.")
        except ValueError:
            continue
        counts[category] += 1
        item = {"url": row[0], "host": host, "title": row[1], "description": row[2],
                "category": category, "category_path": label, "category_id": row[3],
                "category_source": "Curlie human-edited directory"}
        pool = pools[category]
        if len(pool) < flags.count:
            pool.append(item)
        else:
            index = rng.randrange(counts[category])
            if index < flags.count:
                pool[index] = item
    for pool in pools.values():
        rng.shuffle(pool)
    selected, seen = [], set()
    while len(selected) < flags.count:
        progressed = False
        for category, pool in pools.items():
            while pool:
                item = pool.pop()
                if item["host"] in seen:
                    continue
                seen.add(item["host"])
                item["id"] = f"curlie-{len(selected) + 1:05d}"
                selected.append(item)
                progressed = True
                break
            if len(selected) == flags.count:
                break
        if not progressed:
            break
    if len(selected) != flags.count:
        raise RuntimeError(f"Insufficient unique hosts: {len(selected)}/{flags.count}")
    with (output / "urls.jsonl").open("w") as file:
        for item in selected:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")
    config = {
        "viewports": [{"id": "mobile", "width": 390, "height": 844},
                      {"id": "desktop", "width": 1440, "height": 900},
                      {"id": "tablet", "width": 834, "height": 1210}],
        "concurrency": flags.concurrency, "navigationTimeoutMs": 30000,
        "settleMs": 300, "maxNodes": 20000, "maxHeight": 8000,
        "captureMode": "initial",
        "artifacts": {"initial": True, "overlayPng": False, "overlaySvg": False},
        "discovery": {"minPages": 2, "maxPages": 3, "maxVisits": 12,
                      "maxCandidates": 60, "menuInteractions": 4,
                      "similarityThreshold": .95, "viewport": "desktop"}}

    def collection(items):
        return {**config, "sources": [{"id": item["id"], "kind": "url", "baseUrl": item["url"],
                                      "name": item["title"], "category": item["category"]}
                                     for item in items],
                "pages": [{"id": item["id"], "source": item["id"], "path": item["url"]}
                          for item in items]}

    write_json(output / "collector.config.json", collection(selected))
    (output / "shards").mkdir(exist_ok=True)
    shards = []
    for start in range(0, len(selected), 100):
        name = f"shards/{start // 100 + 1:03d}.json"
        write_json(output / name, collection(selected[start:start + 100]))
        shards.append({"config": name})
    report = {"provider": "Curlie", "source_url": "https://curlie.org/directory-dl",
              "license_url": "https://curlie.org/docs/en/license.html",
              "category_method": "original human-assigned category; first path component",
              "sampling": "seeded per-category reservoir then category round-robin; unique normalized host",
              "seed": flags.seed, "categories": flags.categories, "eligible_entry_counts": dict(counts),
              "selected": len(selected), "selected_counts": dict(collections.Counter(x["category"] for x in selected)),
              "live_urls_verified": False, "shards": shards}
    write_json(output / "preparation-report.json", report)
    print(json.dumps({"selected": len(selected), "counts": report["selected_counts"]}))


if __name__ == "__main__":
    main()
