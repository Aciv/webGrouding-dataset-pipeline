#!/usr/bin/env python3
"""Measure real homepage throughput through the unmodified production collect CLI."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

PROJECT = Path(__file__).resolve().parents[2]


def process_rss(root_pid):
    """Sum RSS for this collector and its Chromium descendants, never unrelated jobs.

    RSS includes shared mappings more than once; use as a conservative process-tree
    measurement, not unique physical memory. Returns zero on unsupported platforms.
    """
    try:
        rows = subprocess.check_output(["ps", "-eo", "pid=,ppid=,rss="], text=True)
        processes = [tuple(map(int, line.split())) for line in rows.splitlines()]
    except (OSError, subprocess.SubprocessError):
        return 0
    selected = {root_pid}
    while True:
        children = {pid for pid, parent, _ in processes if parent in selected}
        if children <= selected:
            break
        selected |= children
    return sum(rss for pid, _, rss in processes if pid in selected) / 1024


def stop_child(child):
    """Interrupt only the benchmark's process group and reap it, even after timeout."""
    if child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGINT)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def main():
    """Keep screenshot/annotation policies identical across independent concurrency trials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collector", type=Path, default=PROJECT / "dist/cli.js", help="Built CLI entry; may select a saved baseline dist/cli.js")
    parser.add_argument("--export-only", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--sites", type=int, default=8, help="First N entries in the supplied source list")
    parser.add_argument("--concurrencies", type=int, nargs="+", default=[2, 4])
    parser.add_argument("--sources", type=Path, default=PROJECT / "examples/real-url-10.json")
    parser.add_argument("--template", type=Path, default=PROJECT / "examples/curlie-stream-full.json")
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--max-rss-mb", type=float, default=1800,
                        help="Stop a trial when summed collector/Chromium RSS exceeds this bound; 0 disables")
    args = parser.parse_args()
    if args.sites < 1 or any(value < 1 for value in args.concurrencies) or args.timeout_seconds <= 0 or args.max_rss_mb < 0:
        parser.error("Invalid site/concurrency/time/memory limit")
    args.output.mkdir(parents=True, exist_ok=False)
    template = json.loads(args.template.read_text())
    source_config = json.loads(args.sources.read_text())
    sources = source_config["sources"][:args.sites]
    ids = {source["id"] for source in sources}
    pages = [page for page in source_config["pages"] if page["source"] in ids]
    report = {"metric": "complete_three_viewport_pages_per_wall_second",
              "target_pages_per_second": 5, "note": "Same URLs repeated across trials; warm caches/site drift may affect results.",
              "trials": []}
    for ordinal, concurrency in enumerate(args.concurrencies):
        directory = args.output / f"trial-{ordinal + 1}-c{concurrency}"
        directory.mkdir()
        config = {**template, "sources": sources, "pages": pages, "concurrency": concurrency, "exportOnly": args.export_only,
                  "discovery": {**template.get("discovery", {}), "enabled": False}}
        config_file = directory / "config.json"
        config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        command = ["node", str(args.collector.resolve()), "collect", "--input", str(config_file.resolve()),
                   "--output", str((directory / "capture").resolve())]
        started = time.monotonic()
        peak_rss = 0
        stop_reason = None
        print(json.dumps({"type": "benchmark_started", "concurrency": concurrency, "sites": len(sources)}), flush=True)
        with (directory / "collector.log").open("w") as log:
            child = subprocess.Popen(command, cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                while child.poll() is None:
                    peak_rss = max(peak_rss, process_rss(child.pid))
                    if time.monotonic() - started > args.timeout_seconds:
                        stop_reason = "trial_timeout"
                    if args.max_rss_mb and peak_rss > args.max_rss_mb:
                        stop_reason = "process_tree_rss_limit"
                    if stop_reason:
                        break
                    time.sleep(0.5)
            finally:
                stop_child(child)
        elapsed = time.monotonic() - started
        index_file = directory / "capture/batch-index.json"
        index = json.loads(index_file.read_text()) if index_file.exists() else {}
        accepted = [page for page in index.get("pages", []) if page["status"] == "captured"]
        record = {"concurrency": concurrency, "candidates": len(sources), "completed_pages": len(accepted),
                  "images": sum(len(page["observations"]) for page in accepted),
                  "wall_seconds": round(elapsed, 3), "pages_per_second": round(len(accepted) / elapsed, 4),
                  "peak_process_tree_rss_mb": round(peak_rss, 1), "exit_code": child.returncode,
                  "stop_reason": stop_reason, "report_directory": str(directory.resolve())}
        stage_totals = {}
        capture_counts = {}
        for page in accepted:
            for observation in page["observations"]:
                for key, value in observation.get("timings", {}).items():
                    stage_totals[key] = stage_totals.get(key, 0) + value
                for key, value in observation.get("captureCounts", {}).items():
                    capture_counts[key] = capture_counts.get(key, 0) + value
        visit_totals = {}
        for line in (directory / "collector.log").read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "url_visit":
                for key, value in event.get("timings", {}).items():
                    visit_totals[key] = visit_totals.get(key, 0) + value
        record["capture_stage_ms"] = {k: round(v, 1) for k, v in stage_totals.items()}
        record["visit_stage_ms"] = {k: round(v, 1) for k, v in visit_totals.items()}
        record["capture_counts"] = capture_counts
        record["collector"] = str(args.collector.resolve())
        record["export_only"] = args.export_only
        record["meets_target"] = record["pages_per_second"] >= 5
        report["trials"].append(record)
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"type": "benchmark_finished", **record}), flush=True)


if __name__ == "__main__":
    main()
