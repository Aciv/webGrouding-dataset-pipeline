#!/usr/bin/env python3
"""Independent offline layer normalization; writes derived PNG/JSON pairs and audit reports."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shutil
import time
from grouping_vlm import Client
from grouping_pipeline import process, PIPELINE_VERSION
from storage import write



def main():
    """Run bounded per-observation concurrency against an explicitly selected offline dataset."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Only propose geometry edits; no API and no training output")
    parser.add_argument("--limit", type=int, default=0, help="0 means all observations")
    parser.add_argument("--reuse-edits", type=Path, help="Reuse a same-version grouping report without another API call")
    parser.add_argument("--concurrency", type=int, help="Override sample concurrency from config (1..32)")
    parser.add_argument("--resume", action="store_true", help="Skip structurally accepted outputs; retry failures using cached grouping decisions")
    parser.add_argument("--select", action="append", default=[], help="Select observation directory relative to input; repeat for multiple samples")
    parser.add_argument("--context-mode", choices=("full", "delta"), help="Legacy compatibility option; grouping always sends original evidence and candidates once")
    args = parser.parse_args()
    source, output = args.input.resolve(), args.output.resolve()
    if not source.exists():
        parser.error("Input does not exist")
    source_root = source if source.is_dir() else source.parent
    if output == source_root or output in source_root.parents or source_root in output.parents:
        parser.error("Output must be separate from the source tree")
    if output.exists() and any(output.iterdir()) and not args.resume:
        parser.error("Output must be empty; choose a new output")
    config = json.loads(args.config.read_text())
    if args.context_mode:
        config.setdefault("api", {})["context_mode"] = args.context_mode
    concurrency = args.concurrency if args.concurrency is not None else config.get("concurrency", 1)
    if type(concurrency) is not int or not 1 <= concurrency <= 32 or args.limit < 0:
        parser.error("concurrency must be 1..32; limit must be nonnegative")
    if not args.dry_run:
        Client(config["api"])
    files = sorted(source.rglob("hierarchy.json")) if source.is_dir() else [source]
    files = [f for f in files if not f.is_symlink() and not any(p.startswith(".") for p in f.relative_to(source_root).parts)]
    if args.select:
        requested = set(args.select)
        matched = {f.parent.relative_to(source_root).as_posix() for f in files} & requested
        if matched != requested:
            parser.error("Selected observation(s) not found: "+str(sorted(requested-matched)))
        files = [f for f in files if f.parent.relative_to(source_root).as_posix() in requested]
    if args.limit:
        files = files[:args.limit]
    if not files:
        parser.error("No hierarchy.json files found")
    started = time.monotonic()
    records = []
    with ThreadPoolExecutor(max_workers=concurrency) as workers:
        def previous(file):
            if args.reuse_edits is None:
                return None
            relative = file.relative_to(source) if source.is_dir() else Path(file.name)
            return args.reuse_edits.resolve()/relative.parent/"layer-review.json"
        futures = []
        for file in files:
            relative = file.relative_to(source) if source.is_dir() else Path(file.name)
            audit_file = output/relative.parent/"layer-review.json"
            reuse = previous(file)
            if args.resume and audit_file.exists():
                old = json.loads(audit_file.read_text())
                if Path(old["source"]).resolve() != file.resolve():
                    parser.error("Resume source mismatch")
                same_version = old.get("postprocess_version") == PIPELINE_VERSION
                if same_version and old.get("status") == "accepted" and audit_file.with_name("hierarchy.json").exists():
                    records.append({k: old[k] for k in ("source", "status", "seconds", "before", "after", "rule_summary") if k in old})
                    print(json.dumps({"type": "resume_skip", "source": str(file)}, ensure_ascii=False), flush=True)
                    continue
                if same_version and old.get("status") == "failed" and isinstance(old.get("group_response", {}).get("groups"), list):
                    reuse = audit_file
                shutil.copyfile(audit_file, audit_file.with_name("layer-review.previous.json"))
                if audit_file.with_name("hierarchy.json").exists():
                    audit_file.with_name("hierarchy.json").replace(audit_file.with_name("hierarchy.previous.json"))
            futures.append(workers.submit(process, file, source, output, config, args.dry_run, reuse))
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    report = {"status": "completed" if all(r["status"] in ("accepted", "proposed") for r in records) else "partial",
        "seconds": round(time.monotonic()-started, 3), "observations": records,
        "counts": {s: sum(r["status"] == s for r in records) for s in ("accepted", "proposed", "needs_review", "failed")}}
    write(output/"layer-postprocess-report.json", report)
    if report["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
