#!/usr/bin/env python3
"""Index-first dataset pipeline: filter, postprocess, final filter and pack."""
import argparse
import faulthandler
import json
import math
import signal
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pipeline import index as pipeline_index
from pipeline.filter import filter_index
from page_filter.rules import Rules


def add_rules_arguments(parser):
    """Expose the same geometry thresholds as the standalone filter."""
    parser.add_argument("--min-boxes", type=int, default=10)
    parser.add_argument("--min-outside-boxes", type=int, default=5)
    parser.add_argument("--max-outside-ratio", type=float, default=0.2)
    parser.add_argument("--tolerance-px", type=float, default=2)
    parser.add_argument("--check-layout-overflow", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--min-layout-overflow-ratio", type=float, default=0.1)
    parser.add_argument("--workers", "--filter-workers", dest="workers", type=int, default=8,
                        help="Parallel index/filter workers (local I/O; not VLM concurrency)")


def rules_from(args, layout_policy="reject"):
    """Validate thresholds and build the immutable rule set stored in the index."""
    if min(args.min_boxes, args.min_outside_boxes, args.workers) < 1:
        raise ValueError("Counts and workers must be positive")
    if not math.isfinite(args.tolerance_px) or args.tolerance_px < 0:
        raise ValueError("tolerance-px must be finite and nonnegative")
    if not all(math.isfinite(n) and 0 < n <= 1 for n in (args.max_outside_ratio, args.min_layout_overflow_ratio)):
        raise ValueError("Ratios must be in (0,1]")
    return Rules(args.min_boxes, args.min_outside_boxes, args.max_outside_ratio,
                 args.tolerance_px, args.check_layout_overflow, args.min_layout_overflow_ratio,
                 layout_policy)


def add_viewports(parser):
    parser.add_argument("--viewports", nargs="+", default=None,
                        help="Required viewports; defaults to the index or desktop mobile tablet")


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def command_filter(args):
    """Build an index from original runs, or re-filter an existing index."""
    rules = rules_from(args)
    viewports = args.viewports or ["desktop", "mobile", "tablet"]
    if args.input:
        index = pipeline_index.build_index(args.input, viewports, progress=True,
                                           workers=args.workers)
    else:
        index = pipeline_index.load_index(args.index)
        viewports = args.viewports or index["required_viewports"]
        index["parent"] = pipeline_index.parent_record(args.index)
    index, summary = filter_index(index, rules, viewports, workers=args.workers,
                                  stage="filtered", progress=True)
    output = pipeline_index.write_index(args.output, index, overwrite=args.overwrite)
    emit({"type": "pipeline_filter_completed", **summary, "output": str(output)})


def command_final_filter(args):
    """Validate derived hierarchies and publish the final index consumed by packing."""
    index = pipeline_index.load_index(args.index)
    if not index.get("hierarchies_root"):
        raise ValueError("Final filtering requires a postprocessed index with hierarchies_root")
    rules = rules_from(args, args.layout_overflow_policy)
    viewports = args.viewports or index["required_viewports"]
    index["parent"] = pipeline_index.parent_record(args.index)
    index, summary = filter_index(index, rules, viewports, workers=args.workers,
                                  stage="final", progress=True)
    output = pipeline_index.write_index(args.output, index, overwrite=args.overwrite)
    emit({"type": "pipeline_final_filter_completed", **summary, "output": str(output)})


def command_postprocess(args):
    """Process filter-valid pages and publish the postprocessed index."""
    from pipeline.postprocess import run_index

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.pixel_concurrency is not None:
        if args.pixel_concurrency < 0:
            raise ValueError("pixel-concurrency must be nonnegative")
        config.setdefault("pixels", {})["pixel_concurrency"] = args.pixel_concurrency
    index, summary = run_index(
        args.index, args.index_output, args.hierarchy_root, args.processing_output,
        config, limit=args.limit, concurrency=args.concurrency, resume=args.resume,
        progress=True)
    output = pipeline_index.write_index(args.index_output, index, overwrite=args.resume)
    emit({**summary, "output": str(output)})


def command_pack(args):
    """Package original screenshots and the index-selected hierarchies into chunks."""
    from hf_chunks.pack import pack_index

    result = pack_index(args.index, args.output, chunk_size_mb=args.chunk_size_mb,
                        seed=args.seed, require_url=args.require_url,
                        dedupe_by=args.dedupe_by, resume=args.resume, progress=True,
                        workers=args.workers)
    emit(result)


def _parent_sha(index):
    return (index.get("parent") or {}).get("sha256")


def _retryable_postprocess(index, limit=0):
    """True when failed observations exist, or a previous limit left pages pending."""
    for page in index["pages"]:
        if limit == 0 and page.get("status") == "pending_postprocess":
            return True
        if any(record.get("postprocess", {}).get("status") == "failed"
               for record in page["viewports"].values()):
            return True
    return False


def _package_matches(manifest, final_sha, args, workers):
    return (manifest.get("status") == "completed"
            and (manifest.get("pipeline_index") or {}).get("sha256") == final_sha
            and manifest.get("chunk_size_mb") == args.chunk_size_mb
            and manifest.get("seed") == args.seed
            and (manifest.get("dedupe") or {}).get("by") == args.dedupe_by
            and manifest.get("pack_workers", 1) == workers
            and (not args.require_url or manifest.get("missing_url", 0) == 0))


def _select_pack_output(base, final_path, args, workers):
    """Find a completed matching package or the next free sibling directory."""
    final_sha = pipeline_index.sha256_file(final_path)
    candidates = [base] + [base.with_name(f"{base.name}-{number}") for number in range(2, 1000)]
    free = None
    for candidate in candidates:
        if not candidate.exists() or not any(candidate.iterdir()):
            if free is None:
                free = candidate
            continue
        manifest_file = candidate / "manifest.json"
        if not manifest_file.is_file():
            continue
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _package_matches(manifest, final_sha, args, workers):
            return candidate, True
    if free is None:
        raise ValueError("No free package output directory remains; pass --pack-output")
    return free, False


def command_run(args):
    """Run filter -> postprocess -> final filter -> pack under one work directory.

    ``--resume`` reuses completed stages, retries failed VLM observations, and skips
    packaging when the final index and chunk options already match.
    """
    from pipeline.postprocess import run_index
    from hf_chunks.pack import pack_index

    workdir = Path(args.workdir).resolve()
    if workdir.exists():
        if any(workdir.iterdir()) and not args.resume:
            raise ValueError(f"Work directory must be empty (or pass --resume): {workdir}")
    else:
        workdir.mkdir(parents=True)
    rules = rules_from(args)
    final_rules = rules_from(args, getattr(args, "final_layout_overflow_policy", "warn"))
    viewports = args.viewports or ["desktop", "mobile", "tablet"]
    roots = pipeline_index.resolve_inputs(args.input)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.pixel_concurrency is not None:
        if args.pixel_concurrency < 0:
            raise ValueError("pixel-concurrency must be nonnegative")
        config.setdefault("pixels", {})["pixel_concurrency"] = args.pixel_concurrency

    filtered_path = workdir / "index-filtered.json"
    post_path = workdir / "index-postprocessed.json"
    final_path = workdir / "index-final.json"
    derived_root = workdir / "hierarchies"
    processing_root = workdir / "processing"
    chunks = Path(args.pack_output).resolve() if args.pack_output else workdir / "chunks"

    if args.resume and filtered_path.is_file():
        index = pipeline_index.load_index(filtered_path)
        expected_roots = [str(root) for root in roots]
        if (index.get("stage") != "filtered"
                or [run["root"] for run in index["runs"]] != expected_roots
                or index.get("required_viewports") != viewports
                or index.get("rules") != rules.to_json()):
            raise ValueError("index-filtered.json does not match this run's inputs, "
                             "viewports or rules; choose a new workdir")
        emit({"type": "pipeline_stage", "stage": "filter", "status": "skipped",
              "output": str(filtered_path)})
    else:
        emit({"type": "pipeline_stage", "stage": "filter", "status": "started"})
        index = pipeline_index.build_index(roots, viewports, progress=True,
                                           workers=args.workers)
        index, filter_summary = filter_index(index, rules, viewports, workers=args.workers,
                                             stage="filtered", progress=True)
        filtered_path = pipeline_index.write_index(filtered_path, index, overwrite=args.resume)
        emit({"type": "pipeline_filter_completed", **filter_summary,
              "output": str(filtered_path)})
    if getattr(args, "stop_after", "pack") == "filter":
        emit({"type": "pipeline_stopped", "stage": "filter",
              "output": str(filtered_path)})
        return

    post_reused = False
    if args.resume and post_path.is_file():
        post_index = pipeline_index.load_index(post_path)
        same_parent = _parent_sha(post_index) == pipeline_index.sha256_file(filtered_path)
        if (post_index.get("stage") == "postprocessed" and same_parent
                and not _retryable_postprocess(post_index, args.limit)):
            emit({"type": "pipeline_stage", "stage": "postprocess", "status": "skipped",
                  "output": str(post_path)})
            index = post_index
            post_reused = True
        else:
            emit({"type": "pipeline_stage", "stage": "postprocess", "status": "resuming",
                  "output": str(post_path)})
    if not post_reused:
        if args.resume and post_path.is_file():
            index, post_summary = run_index(
                filtered_path, post_path, derived_root, processing_root, config,
                limit=args.limit, concurrency=args.concurrency, resume=True, progress=True)
        else:
            emit({"type": "pipeline_stage", "stage": "postprocess", "status": "started"})
            index, post_summary = run_index(
                filtered_path, post_path, derived_root, processing_root, config,
                limit=args.limit, concurrency=args.concurrency, resume=args.resume,
                progress=True)
        post_path = pipeline_index.write_index(post_path, index, overwrite=args.resume)
        emit({**post_summary, "output": str(post_path)})
    if getattr(args, "stop_after", "pack") == "postprocess":
        emit({"type": "pipeline_stopped", "stage": "postprocess",
              "output": str(post_path)})
        return

    index["parent"] = pipeline_index.parent_record(post_path)
    if args.resume and final_path.is_file():
        final_index = pipeline_index.load_index(final_path)
        current = (final_index.get("stage") == "final"
                   and _parent_sha(final_index) == pipeline_index.sha256_file(post_path)
                   and final_index.get("required_viewports") == viewports
                   and final_index.get("rules") == final_rules.to_json())
        if current:
            emit({"type": "pipeline_stage", "stage": "final_filter", "status": "skipped",
                  "output": str(final_path)})
        else:
            final_index, final_summary = filter_index(index, final_rules, viewports,
                                                      workers=args.workers, stage="final",
                                                      progress=True)
            final_path = pipeline_index.write_index(final_path, final_index, overwrite=True)
            emit({"type": "pipeline_final_filter_completed", **final_summary,
                  "output": str(final_path)})
    else:
        final_index, final_summary = filter_index(index, final_rules, viewports,
                                                  workers=args.workers, stage="final",
                                                  progress=True)
        final_path = pipeline_index.write_index(final_path, final_index, overwrite=args.resume)
        emit({"type": "pipeline_final_filter_completed", **final_summary,
              "output": str(final_path)})
    if getattr(args, "stop_after", "pack") == "final-filter":
        emit({"type": "pipeline_stopped", "stage": "final-filter",
              "output": str(final_path)})
        return

    if args.pack_output is None:
        original_chunks = chunks
        chunks, reusable = _select_pack_output(chunks, final_path, args, args.pack_workers)
        if not reusable and original_chunks.exists() and any(original_chunks.iterdir()):
            emit({"type": "pipeline_stage", "stage": "pack", "status": "new_output",
                  "reason": "existing package does not match the current final index",
                  "output": str(chunks)})
    result = pack_index(final_path, chunks, chunk_size_mb=args.chunk_size_mb,
                        seed=args.seed, require_url=args.require_url,
                        dedupe_by=args.dedupe_by, resume=args.resume, progress=True,
                        workers=args.pack_workers)
    emit(result)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    filtering = subparsers.add_parser("filter", help="Build/filter an index from original runs")
    source = filtering.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", nargs="+", type=Path, help="One or more original collection roots")
    source.add_argument("--index", type=Path, help="Existing pipeline index to re-filter")
    filtering.add_argument("--output", required=True, type=Path)
    filtering.add_argument("--overwrite", action="store_true",
                           help="Repeat a completed filter stage in place")
    add_viewports(filtering)
    add_rules_arguments(filtering)
    filtering.set_defaults(func=command_filter)

    final = subparsers.add_parser("final-filter", help="Validate derived hierarchies")
    final.add_argument("--index", required=True, type=Path)
    final.add_argument("--output", required=True, type=Path)
    final.add_argument("--overwrite", action="store_true",
                       help="Repeat a completed final-filter stage in place")
    final.add_argument("--layout-overflow-policy", choices=("reject", "warn", "ignore"),
                       default="warn",
                       help="Layout overflow on clean labels: reject, warn only, or ignore (default warn)")
    add_viewports(final)
    add_rules_arguments(final)
    final.set_defaults(func=command_final_filter)

    post = subparsers.add_parser("postprocess", help="Create a derived hierarchy version")
    post.add_argument("--index", required=True, type=Path)
    post.add_argument("--index-output", required=True, type=Path)
    post.add_argument("--hierarchy-root", required=True, type=Path)
    post.add_argument("--processing-output", required=True, type=Path)
    post.add_argument("--config", required=True, type=Path)
    post.add_argument("--limit", type=int, default=0, help="Maximum pages; 0 means all")
    post.add_argument("--concurrency", "--vlm-concurrency", dest="concurrency", type=int,
                      help="VLM concurrency (1..256); bound pixel decoding with pixels.pixel_concurrency")
    post.add_argument("--pixel-concurrency", type=int,
                      help="Override config pixels.pixel_concurrency (0 disables the separate bound)")
    post.add_argument("--resume", action="store_true")
    post.set_defaults(func=command_postprocess)

    packing = subparsers.add_parser("pack", help="Package an index into compressed chunks")
    packing.add_argument("--index", required=True, type=Path)
    packing.add_argument("--output", required=True, type=Path)
    packing.add_argument("--chunk-size-mb", type=float, default=1024)
    packing.add_argument("--dedupe-by", choices=("url", "site", "none"), default="url")
    packing.add_argument("--seed", type=int, default=42)
    packing.add_argument("--require-url", action="store_true")
    packing.add_argument("--workers", type=int, default=1,
                         help="Parallel shard writers; >1 changes shard names to train-NN-xxxxx")
    packing.add_argument("--resume", action="store_true",
                         help="Reuse a completed package with the same index and options")
    packing.set_defaults(func=command_pack)

    run = subparsers.add_parser("run", help="Run filter, postprocess, final filter and pack")
    run.add_argument("--input", nargs="+", required=True, type=Path)
    run.add_argument("--workdir", required=True, type=Path)
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--pack-output", type=Path)
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--concurrency", "--vlm-concurrency", dest="concurrency", type=int,
                     help="VLM concurrency (1..256); pixel decoding is separately bounded by pixels.pixel_concurrency")
    run.add_argument("--pixel-concurrency", type=int,
                     help="Override config pixels.pixel_concurrency (0 disables the separate bound)")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--chunk-size-mb", type=float, default=1024)
    run.add_argument("--dedupe-by", choices=("url", "site", "none"), default="url")
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--require-url", action="store_true")
    run.add_argument("--pack-workers", type=int, default=1,
                     help="Parallel shard writers for the packaging stage")
    run.add_argument("--stop-after", choices=("filter", "postprocess", "final-filter", "pack"),
                     default="pack", help="Stop after this completed stage; resume later with --resume")
    run.add_argument("--final-layout-overflow-policy", choices=("reject", "warn", "ignore"),
                     default="warn",
                     help="Layout overflow policy for the final filter (default warn)")
    add_viewports(run)
    add_rules_arguments(run)
    run.set_defaults(func=command_run)
    return parser


def main():
    try:
        faulthandler.register(signal.SIGUSR1)
    except (AttributeError, ValueError, RuntimeError):
        pass
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        parser.exit(1, f"Pipeline failed: {error}\n")


if __name__ == "__main__":
    main()
