#!/usr/bin/env python3
"""Collect categorized Curlie candidates until a successful responsive-page target is met."""
import argparse
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "scripts"))
from pool import CandidatePool, DEFAULT_CATEGORIES, build_pool
from storage import read_json
from runner import run_collection


def main():
    """Validate scheduling settings and reuse the existing full-capture template."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, default=PROJECT / "examples/curlie-categorized/pool.sqlite")
    parser.add_argument("--archive", type=Path, default=PROJECT / "examples/curlie-categorized/curlie-rdf-all.tar.gz")
    parser.add_argument("--config", type=Path, default=PROJECT / "examples/curlie-stream-full.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-pages", type=int, default=10000)
    parser.add_argument("--batch-sites", type=int, default=24, help="Candidate prefetch chunk size; not a scheduling barrier")
    parser.add_argument("--concurrency", type=int, default=12, help="Global active site/context limit in one shared Chromium")
    parser.add_argument("--site-timeout-seconds", type=float, default=180, help="Per-site wall deadline; closes only that context and refills its slot")
    parser.add_argument("--browser-restarts", type=int, default=2, help="Maximum browser/executor restarts per invocation; exhausted jobs stay pending")
    parser.add_argument("--explore-pages", action=argparse.BooleanOptionalAction, default=None,
                        help="Explore child pages; default reads discovery.enabled (false if omitted)")
    parser.add_argument("--max-pages-per-site", type=int, default=3)
    parser.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES)
    parser.add_argument("--export-only", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-history", type=Path, nargs="+", default=[],
                        help="Prior Curlie run directories or collection-index.json files; skip all claimed hosts")
    args = parser.parse_args()
    if min(args.target_pages, args.batch_sites, args.concurrency, args.max_pages_per_site) < 1:
        parser.error("Page target, batch size, concurrency and page limit must be positive")
    if not 0 < args.site_timeout_seconds < float("inf"):
        parser.error("site-timeout-seconds must be positive and finite")
    if args.browser_restarts < 0:
        parser.error("browser-restarts must be nonnegative")
    if not 2 <= args.max_pages_per_site <= 5:
        parser.error("max-pages-per-site must be 2–5, matching the existing collector")
    if not (PROJECT / "dist/application/discovery/stream-worker.js").is_file():
        parser.error("Run npm run build first")
    if not args.pool.exists():
        if not args.archive.is_file():
            parser.error("Download https://curlie.org/directory-dl using --archive, or supply an existing --pool")
        print('{"type":"pool_build_started"}', flush=True)
        build_pool(args.archive, args.pool)
    pool = CandidatePool(args.pool)
    try:
        if any(category not in pool.counts for category in args.categories):
            parser.error(f"Available pool categories: {list(pool.counts)}")
        config = read_json(args.config)
        template = {k: v for k, v in config.items() if k not in ("sources", "pages", "concurrency")}
        discovery = template.setdefault("discovery", {})
        enabled = discovery.get("enabled", False) if args.explore_pages is None else args.explore_pages
        if not isinstance(enabled, bool):
            parser.error("discovery.enabled must be boolean")
        discovery["enabled"] = enabled
        if not enabled:
            discovery.update(minPages=1, maxPages=1, maxVisits=1, menuInteractions=0)
        settings = {"pool": str(args.pool.resolve()), "poolCounts": pool.counts,
                    "targetPages": args.target_pages, "batchSites": args.batch_sites,
                    "concurrency": args.concurrency, "maxPagesPerSite": args.max_pages_per_site,
                    "siteTimeoutSeconds": args.site_timeout_seconds,
                    "browserRestarts": args.browser_restarts,
                    "categories": list(dict.fromkeys(args.categories)), "exportOnly": args.export_only,
                    "template": template}
        if args.skip_history:
            settings["skipHistory"] = [str(file.resolve()) for file in args.skip_history]
        return run_collection(PROJECT, pool, args.output.resolve(), settings, template, args.resume)
    finally:
        pool.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(f"Collection failed: {error}", file=sys.stderr)
        sys.exit(1)
