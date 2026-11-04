#!/usr/bin/env python3
"""Build reproducible initial-only collection batches from a cached public ranking.

This prepares candidates, not verified annotations. HTTP probes only normalize entry
redirects; a successful probe does not imply Chromium or responsive capture success.
"""

import argparse
import concurrent.futures
import csv
import datetime as dt
import io
import json
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DOWNLOAD = "https://downloads.majestic.com/majestic_million.csv"
REPORT = "https://majestic.com/reports/majestic-million"
INFRASTRUCTURE = {
    "googleapis.com", "gstatic.com", "googleusercontent.com", "googletagmanager.com",
    "googlesyndication.com", "googleadservices.com", "doubleclick.net", "fbcdn.net",
    "akamai.net", "akamaized.net", "cloudfront.net", "amazonaws.com", "azureedge.net",
    "windows.net", "trafficmanager.net", "fastly.net", "jsdelivr.net", "unpkg.com",
    "w3.org", "schema.org", "purl.org", "doi.org", "dx.doi.org", "t.co", "bit.ly",
}


def now():
    """Return an explicit UTC timestamp for source and preparation provenance."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(file, value):
    """Write readable UTF-8 metadata, creating its parent directory if needed."""
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def snapshot(directory, rank_limit):
    """Cache a bounded ranking prefix; never silently refresh an existing snapshot."""
    target = directory / "majestic-prefix.csv"
    metadata = directory / "ranking-source.json"
    if target.exists():
        if not metadata.exists():
            raise ValueError("Cached ranking is missing ranking-source.json")
        previous = json.loads(metadata.read_text())
        if previous["rank_limit"] < rank_limit:
            raise ValueError("Cached prefix is too short; choose a new output directory")
        return target, previous
    directory.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(DOWNLOAD, headers={"User-Agent": "ResponsiveUIDataset/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        reader = csv.DictReader(io.TextIOWrapper(response, encoding="utf-8-sig"))
        rows = []
        for row in reader:
            if int(row["GlobalRank"]) > rank_limit:
                break
            rows.append(row)
        info = {"provider": "Majestic Million", "url": DOWNLOAD, "description_url": REPORT,
                "license": "CC BY 3.0 (ranking only; not website contents)",
                "retrieved_at": now(), "http_last_modified": response.headers.get("Last-Modified"),
                "rank_limit": rank_limit, "rows": len(rows), "prefix_only": True}
        with target.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    write_json(metadata, info)
    return target, info


def candidate_pool(file, rank_limit=None):
    """Deduplicate hostnames and remove explicit infrastructure/non-HTML entry hosts.

    Parent-host collapsing is a conservative hostname suffix check, not a public
    suffix / ownership classifier. Categories and page quality remain unverified.
    """
    candidates, exclusions, seen = [], [], set()
    with file.open() as source:
        for row in csv.DictReader(source):
            if rank_limit is not None and int(row["GlobalRank"]) > rank_limit:
                break
            host = row["Domain"].lower().rstrip(".").removeprefix("www.")
            parts = host.split(".")
            reason = None
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,63}", host):
                reason = "unsupported_hostname"
            elif any(host == item or host.endswith("." + item) for item in INFRASTRUCTURE):
                reason = "explicit_infrastructure_host"
            elif parts[0] in {"api", "cdn", "static", "assets", "img", "images", "mail", "smtp", "ns1", "ns2"}:
                reason = "infrastructure_prefix"
            elif host in seen or any(".".join(parts[i:]) in seen for i in range(1, len(parts) - 1)):
                reason = "duplicate_or_child_hostname"
            if reason:
                exclusions.append({"host": host, "rank": int(row["GlobalRank"]), "reason": reason})
                continue
            seen.add(host)
            candidates.append({"host": host, "rank": int(row["GlobalRank"]),
                               "entry_url": f"https://{host}/"})
    return candidates, exclusions


def probe(item, timeout):
    """Resolve HTTP redirects without downloading a page body; preserve failed candidates.

    Record only HTTP(S) final URLs with an HTML content type. Failures remain visible
    in the candidate ledger and retain the original HTTPS entry for Chromium to try.
    """
    result = dict(item)
    request = urllib.request.Request(item["entry_url"], headers={"User-Agent": "Mozilla/5.0 ResponsiveUIDataset/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = response.url
            parsed = urllib.parse.urlsplit(final)
            content_type = response.headers.get("Content-Type", "")
            result["probe"] = {"checked_at": now(), "status": response.status,
                               "final_url": final, "content_type": content_type}
            if parsed.scheme in {"https", "http"} and "html" in content_type.lower():
                result["entry_url"] = final.split("#", 1)[0]
                result["probe"]["outcome"] = "html_headers_received"
            else:
                result["probe"]["outcome"] = "unverified_content_type"
    except Exception as error:
        result["probe"] = {"checked_at": now(), "outcome": "failed", "error": str(error)}
    return result


def config(items, concurrency):
    """Generate the existing collector protocol, with concurrency across websites only."""
    sources, pages = [], []
    for item in items:
        identifier = f"majestic-{item['rank']:06d}-" + re.sub(r"[^a-z0-9-]", "-", item["host"])
        parsed = urllib.parse.urlsplit(item["entry_url"])
        sources.append({"id": identifier, "kind": "url", "baseUrl": f"{parsed.scheme}://{parsed.netloc}/",
                        "category": "Unclassified", "name": item["host"]})
        pages.append({"id": identifier, "source": identifier,
                      "path": (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")})
    return {"viewports": [{"id": "mobile", "width": 390, "height": 844},
                          {"id": "desktop", "width": 1440, "height": 900},
                          {"id": "tablet", "width": 834, "height": 1210}],
            "concurrency": concurrency, "navigationTimeoutMs": 30000, "settleMs": 300,
            "maxNodes": 20000, "maxHeight": 8000, "captureMode": "initial",
            "artifacts": {"initial": True, "overlayPng": False, "overlaySvg": False},
            "discovery": {"minPages": 2, "maxPages": 3, "maxVisits": 12, "maxCandidates": 60,
                          "menuInteractions": 4, "similarityThreshold": 0.95, "viewport": "desktop"},
            "sources": sources, "pages": pages}


def main():
    """Prepare ranked candidates, optional cached probes, one full config and bounded shards."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("examples/ranked-2000"))
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--rank-limit", type=int, default=10000)
    parser.add_argument("--concurrency", type=int, default=12, help="Simultaneous website jobs in the collector")
    parser.add_argument("--shard-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--probe", action="store_true", help="Check headers/entry redirects; not a browser quality check")
    parser.add_argument("--require-html", action="store_true", help="Fill from reserves until selected entries have HTML response headers")
    parser.add_argument("--probe-workers", type=int, default=32)
    parser.add_argument("--probe-timeout", type=float, default=8)
    args = parser.parse_args()
    if min(args.count, args.rank_limit, args.concurrency, args.shard_size, args.probe_workers, args.probe_timeout) <= 0:
        parser.error("Counts, timeouts and concurrency must be positive")
    if args.require_html and not args.probe:
        parser.error("--require-html requires --probe")
    ranking, provenance = snapshot(args.output, args.rank_limit)
    candidates, exclusions = candidate_pool(ranking, args.rank_limit)
    # Shuffle within ten equal rank bands and interleave: avoid exhausting only the highest ranks first.
    rng = random.Random(args.seed)
    bands = [[] for _ in range(10)]
    for item in candidates:
        bands[min(9, (item["rank"] - 1) * 10 // args.rank_limit)].append(item)
    for band in bands:
        rng.shuffle(band)
    ordered = [band[i] for i in range(max(map(len, bands))) for band in bands if i < len(band)]
    if len(ordered) < args.count:
        raise ValueError(f"Only {len(ordered)} candidates; requested {args.count}")
    selected = ordered[:args.count]
    probe_cache = args.output / "http-probes.json"
    cache = json.loads(probe_cache.read_text()) if probe_cache.exists() else {}
    if args.probe:
        todo = [item for item in selected if item["host"] not in cache]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.probe_workers) as executor:
            futures = [executor.submit(probe, item, args.probe_timeout) for item in todo]
            for number, future in enumerate(concurrent.futures.as_completed(futures), 1):
                item = future.result()
                cache[item["host"]] = item
                if number % 100 == 0 or number == len(todo):
                    write_json(probe_cache, cache)
                    print(json.dumps({"type": "probe_progress", "completed": number, "total": len(todo)}), flush=True)
        selected = [cache[item["host"]] for item in selected]
    if args.require_html:
        # Failed probes stay in the ledger/reserve, but do not consume the active
        # 2,000-site quota. This remains a headers check, not visual validation.
        while True:
            ready, ready_origins = [], set()
            for item in ordered:
                result = cache.get(item["host"])
                if not result or result["probe"]["outcome"] != "html_headers_received":
                    continue
                parsed = urllib.parse.urlsplit(result["entry_url"])
                origin = (parsed.scheme, parsed.netloc.lower())
                if origin not in ready_origins:
                    ready_origins.add(origin)
                    ready.append(result)
            if len(ready) >= args.count:
                selected = ready[:args.count]
                break
            todo = [item for item in ordered if item["host"] not in cache][:256]
            if not todo:
                raise ValueError(f"Only {len(ready)} distinct HTML-header entries; expand --rank-limit in a new output")
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.probe_workers) as executor:
                futures = [executor.submit(probe, item, args.probe_timeout) for item in todo]
                for future in concurrent.futures.as_completed(futures):
                    item = future.result()
                    cache[item["host"]] = item
            write_json(probe_cache, cache)
            print(json.dumps({"type": "reserve_probe_progress", "distinct_html_before_batch": len(ready),
                              "checked_total": len(cache), "target": args.count}), flush=True)
    # Several ranking hosts can redirect to the same service. Keep one configured
    # origin and fill the quota from reserves; reserve replacements are not probed here.
    unique, origins = [], set()
    for item in selected + ([] if args.require_html else ordered[args.count:]):
        origin = urllib.parse.urlsplit(item["entry_url"])
        key = (origin.scheme, origin.netloc.lower())
        if key in origins:
            continue
        origins.add(key)
        unique.append(item)
        if len(unique) == args.count:
            break
    if len(unique) != args.count:
        raise ValueError("Not enough distinct origins after HTTP redirect normalization")
    selected = unique
    selected_hosts = {item["host"] for item in selected}
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "candidate-pool.jsonl").open("w") as output:
        for item in ordered:
            output.write(json.dumps({**cache.get(item["host"], item), "selected": item["host"] in selected_hosts,
                                     "provider": "Majestic Million", "annotation_status": "unverified"}) + "\n")
    write_json(args.output / "excluded-hosts.json", exclusions)
    write_json(args.output / "collector.config.json", config(selected, args.concurrency))
    shards = []
    for start in range(0, len(selected), args.shard_size):
        name = f"shards/{start // args.shard_size + 1:03d}.json"
        subset = selected[start:start + args.shard_size]
        write_json(args.output / name, config(subset, args.concurrency))
        shards.append({"config": name, "sources": len(subset)})
    outcomes = {}
    for item in selected:
        key = item.get("probe", {}).get("outcome", "not_probed")
        outcomes[key] = outcomes.get(key, 0) + 1
    summary = {"created_at": now(), "ranking": provenance, "seed": args.seed,
               "selection": "seeded shuffle within 10 rank bands, round-robin across bands",
               "require_html_headers": args.require_html,
               "selected": len(selected), "reserve": len(ordered) - len(selected), "excluded": len(exclusions),
               "unique_config_origins": len({source["baseUrl"] for source in config(selected, args.concurrency)["sources"]}),
               "http_probes": outcomes, "browser_validated": False, "concurrency": args.concurrency,
               "image_range_if_every_site_meets_page_quota": [args.count * 2 * 3, args.count * 3 * 3],
               "shards": shards}
    write_json(args.output / "preparation-report.json", summary)
    print(json.dumps({"selected": len(selected), "reserve": summary["reserve"], "probes": outcomes,
                      "config": str(args.output / "collector.config.json"), "shards": len(shards)}), flush=True)


if __name__ == "__main__":
    main()
