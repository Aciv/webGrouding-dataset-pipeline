#!/usr/bin/env python3
"""Current YAML dataset pipeline, with archived index/VLM/chunk commands for legacy data."""
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
PROJECT = SCRIPTS.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

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


def command_dataset(args):
    """Run current staged processing, optionally export portable labels and compress page shards."""
    from pipeline.components import run_component_pipeline
    from pipeline.prepare import prepare_pack_input
    from pipeline.detail_selection import select_detail_crops
    from pipeline.layer_projection import project_final_layers
    from hf_chunks.pack import pack_index
    if args.pack_workers < 1 or not math.isfinite(args.chunk_size_mb) or args.chunk_size_mb <= 0:
        raise ValueError('pack-workers and chunk-size-mb must be positive')
    if not math.isfinite(args.min_detail_coverage) or not 0 <= args.min_detail_coverage <= 1:
        raise ValueError('min-detail-coverage must be in [0,1]')
    stop = args.stop_after
    process_stop = stop if stop in ('filter', 'postprocess') else 'final-filter'
    result = run_component_pipeline(args.input, args.workdir, args.config, args.resume,
         args.limit_pages, args.baseline, args.workers, args.pixel_workers, args.page_ids,
         stop_after=process_stop)
    emit({'type': 'pipeline_stage', 'stage': process_stop,
          'status': 'skipped' if result.get('resumed') else 'completed', **result})
    if stop in ('filter', 'postprocess', 'final-filter'):
        emit({'type': 'pipeline_stopped', 'stage': stop})
        return
    workdir = Path(args.workdir).resolve()
    final_path = Path(result['final_index'])
    layer_output = (Path(args.layer_output).resolve() if args.layer_output else
                    _select_layer_output(workdir.with_name(workdir.name+'-final-l5'),final_path))
    projected = project_final_layers(final_path,layer_output,resume=args.resume,workers=min(args.workers or 8,32))
    emit({'type':'pipeline_stage','stage':'layer-filter',**projected})
    final_path = Path(projected['final_index'])
    if stop == 'layer-filter':
        emit({'type':'pipeline_stopped','stage':stop,'output':str(final_path)})
        return
    detail_output = (Path(args.detail_output).resolve() if args.detail_output else
                     _select_detail_output(workdir.with_name(workdir.name+'-detail-selection'),
                                           final_path, args.min_detail_coverage))
    detail = select_detail_crops(final_path, detail_output, args.min_detail_coverage, resume=args.resume)
    emit({'type':'pipeline_stage', 'stage':'detail-filter', **detail})
    final_path = Path(detail['final_index'])
    from scripts.offline_postprocess.layer_statistics import compute_layer_statistics
    layer_report = detail_output/'reports/layer-statistics.json'
    cached_layers = json.loads(layer_report.read_text()) if layer_report.is_file() else {}
    if cached_layers.get('source_index_sha256') != pipeline_index.sha256_file(final_path):
        compute_layer_statistics(final_path,workers=args.workers or 8)
    if stop == 'detail-filter':
        emit({'type':'pipeline_stopped', 'stage':stop, 'output':str(final_path)})
        return
    prepared = (Path(args.prepare_output).resolve() if args.prepare_output else
                _select_prepare_output(workdir.with_name(workdir.name+'-pack-input'), final_path))
    prepared_result = prepare_pack_input(final_path, prepared, args.workers or 8, resume=args.resume)
    emit({'type': 'pipeline_stage', 'stage': 'prepare-pack', **prepared_result})
    if stop == 'prepare-pack':
        emit({'type': 'pipeline_stopped', 'stage': stop, 'output': prepared_result['pack_index']})
        return
    pack_index_path = Path(prepared_result['pack_index'])
    chunks = (Path(args.pack_output).resolve() if args.pack_output else
              _select_pack_output(workdir.with_name(workdir.name+'-chunks'),
                                  pack_index_path, args, args.pack_workers)[0])
    emit(pack_index(pack_index_path, chunks, chunk_size_mb=args.chunk_size_mb,
                    seed=args.seed, require_url=True, dedupe_by=args.dedupe_by,
                    resume=args.resume, progress=True, workers=args.pack_workers))
    emit({'type': 'pipeline_stopped', 'stage': 'pack', 'output': str(chunks)})


def _select_detail_output(base, final_path, threshold):
    """Find the matching crop-only policy or an unused sibling without overwriting older selections."""
    from pipeline.detail_selection import RULE
    digest = pipeline_index.sha256_file(final_path)
    free = None
    for candidate in [base]+[base.with_name(f'{base.name}-{n}') for n in range(2,1000)]:
        if not candidate.exists() or not any(candidate.iterdir()):
            free = free or candidate
            continue
        path = candidate/'index-final.json'
        if path.is_file():
            policy = json.loads(path.read_text()).get('detail_training_selection',{})
            if (policy.get('source_index_sha256') == digest and policy.get('minimum_target_union_coverage') == threshold
                    and policy.get('rule_version') == RULE):
                return candidate
    if free is None:
        raise ValueError('No free detail-selection output; pass --detail-output')
    return free


def _select_layer_output(base, final_path):
    """Reuse the exact L5 projection parent or choose a fresh sibling directory."""
    digest = pipeline_index.sha256_file(final_path)
    free = None
    for candidate in [base]+[base.with_name(f'{base.name}-{n}') for n in range(2,1000)]:
        if not candidate.exists() or not any(candidate.iterdir()):
            free=free or candidate
            continue
        path=candidate/'index-final.json'
        if path.is_file() and json.loads(path.read_text()).get('final_layer_projection',{}).get('source_index_sha256')==digest:
            return candidate
    if free is None:raise ValueError('No free final-layer output; pass --layer-output')
    return free


def command_project_layers(args):
    """Keep actual L1-L5 graph/crop labels after fusion without changing cuts or source captures."""
    from pipeline.layer_projection import project_final_layers
    emit(project_final_layers(args.index,args.output,args.resume,args.workers))


def command_select_detail(args):
    """Flag low-coverage xyxy crops for detail training only; retain observation and split records."""
    from pipeline.detail_selection import select_detail_crops
    emit(select_detail_crops(args.index, args.output, args.min_coverage, args.resume))


def _select_prepare_output(base, final_path):
    """Reuse an export of the same source index, otherwise choose an unused sibling."""
    digest = pipeline_index.sha256_file(final_path)
    free = None
    for candidate in [base] + [base.with_name(f'{base.name}-{n}') for n in range(2, 1000)]:
        if not candidate.exists() or not any(candidate.iterdir()):
            free = free or candidate
            continue
        manifest = candidate/'manifest.json'
        if manifest.is_file() and (candidate/'pack-index.json').is_file():
            value = json.loads(manifest.read_text())
            if value.get('status') == 'completed' and value.get('source_index_sha256') == digest:
                return candidate
    if free is None:
        raise ValueError('No free prepared output; pass --prepare-output')
    return free


def command_prepare(args):
    """Export accepted component pages as portable image/URL/compact-label packing input."""
    from pipeline.prepare import prepare_pack_input
    emit(prepare_pack_input(args.index, args.output, args.workers, resume=args.resume))


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
    """Dispatch current YAML processing through packing, or explicitly selected legacy VLM stages.

    ``--resume`` validates checkpoints and skips matching completed output. Component
    export/packing preserve page groups and whole-image/crop-local xyxy coordinates.
    """
    if getattr(args, 'engine', 'legacy') == 'components':
        if len(args.input) != 1:
            raise ValueError('The component pipeline currently accepts one --input root')
        args.input = args.input[0]
        args.limit_pages = args.limit
        args.dedupe_by = args.dedupe_by or 'none'
        return command_dataset(args)
    args.dedupe_by = args.dedupe_by or 'url'
    args.workers = args.workers or 8
    if args.stop_after in ('layer-filter', 'detail-filter', 'prepare-pack'):
        raise ValueError('layer-filter/detail-filter/prepare-pack are component stages; use --engine components')
    if not args.config:
        raise ValueError('Legacy engine requires an explicit JSON --config')
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
    dataset = subparsers.add_parser('dataset', help='Current YAML pipeline: pre-filter -> components/slices -> final-filter -> validated index')
    dataset.add_argument('--input', required=True, type=Path)
    dataset.add_argument('--workdir', required=True, type=Path)
    dataset.add_argument('--config', type=Path)
    dataset.add_argument('--baseline', type=Path)
    dataset.add_argument('--resume', action='store_true')
    dataset.add_argument('--limit-pages', type=int, default=0)
    dataset.add_argument('--page-ids', nargs='+')
    dataset.add_argument('--workers', type=int)
    dataset.add_argument('--pixel-workers', type=int)
    dataset.add_argument('--stop-after', choices=('filter', 'postprocess', 'final-filter', 'layer-filter', 'detail-filter', 'prepare-pack', 'pack'),
                         default='detail-filter')
    dataset.add_argument('--prepare-output', type=Path)
    dataset.add_argument('--detail-output', type=Path)
    dataset.add_argument('--layer-output', type=Path)
    dataset.add_argument('--min-detail-coverage', type=float, default=0.2)
    dataset.add_argument('--pack-output', type=Path)
    dataset.add_argument('--pack-workers', type=int, default=8)
    dataset.add_argument('--chunk-size-mb', type=float, default=1024)
    dataset.add_argument('--dedupe-by', choices=('url', 'site', 'none'), default='none')
    dataset.add_argument('--seed', type=int, default=42)
    dataset.set_defaults(require_url=True)
    dataset.set_defaults(func=command_dataset)

    layers = subparsers.add_parser('project-layers',help='Publish true final L1-L5 graph and crop labels after fusion/slicing')
    layers.add_argument('--index',required=True,type=Path)
    layers.add_argument('--output',required=True,type=Path)
    layers.add_argument('--workers',type=int,default=8)
    layers.add_argument('--resume',action='store_true')
    layers.set_defaults(func=command_project_layers)

    detail = subparsers.add_parser('select-detail-crops', help='Flag low-union-coverage crops for detail training only')
    detail.add_argument('--index', required=True, type=Path)
    detail.add_argument('--output', required=True, type=Path)
    detail.add_argument('--min-coverage', type=float, default=0.2)
    detail.add_argument('--resume', action='store_true')
    detail.set_defaults(func=command_select_detail)

    prepare = subparsers.add_parser('prepare-pack', help='Export compact accepted page/image/URL/labels for packing')
    prepare.add_argument('--index', required=True, type=Path)
    prepare.add_argument('--output', required=True, type=Path)
    prepare.add_argument('--workers', type=int, default=8)
    prepare.add_argument('--resume', action='store_true')
    prepare.set_defaults(func=command_prepare)

    filtering = subparsers.add_parser("filter", help="Legacy geometry-only index filter; current staged pipeline: dataset")
    source = filtering.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", nargs="+", type=Path, help="One or more original collection roots")
    source.add_argument("--index", type=Path, help="Existing pipeline index to re-filter")
    filtering.add_argument("--output", required=True, type=Path)
    filtering.add_argument("--overwrite", action="store_true",
                           help="Repeat a completed filter stage in place")
    add_viewports(filtering)
    add_rules_arguments(filtering)
    filtering.set_defaults(func=command_filter)

    final = subparsers.add_parser("final-filter", help="Legacy derived-hierarchy filter")
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

    post = subparsers.add_parser("postprocess", help="Legacy VLM/five-layer hierarchy processing")
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

    packing = subparsers.add_parser("pack", help="Package prepared component pages or legacy indexed pages")
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

    run = subparsers.add_parser("run", help="Full component pipeline through compression; --engine legacy selects archived VLM processing")
    run.add_argument('--engine', choices=('components', 'legacy'), default='components')
    run.add_argument("--input", nargs="+", required=True, type=Path)
    run.add_argument("--workdir", required=True, type=Path)
    run.add_argument("--config", type=Path, help='Component YAML (default bundled); legacy engine requires JSON')
    run.add_argument('--baseline', type=Path)
    run.add_argument('--page-ids', nargs='+')
    run.add_argument('--pixel-workers', type=int)
    run.add_argument('--prepare-output', type=Path)
    run.add_argument('--detail-output', type=Path)
    run.add_argument('--layer-output', type=Path)
    run.add_argument('--min-detail-coverage', type=float, default=0.2)
    run.add_argument("--pack-output", type=Path)
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--concurrency", "--vlm-concurrency", dest="concurrency", type=int,
                     help="VLM concurrency (1..256); pixel decoding is separately bounded by pixels.pixel_concurrency")
    run.add_argument("--pixel-concurrency", type=int,
                     help="Override config pixels.pixel_concurrency (0 disables the separate bound)")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--chunk-size-mb", type=float, default=1024)
    run.add_argument("--dedupe-by", choices=("url", "site", "none"), default=None)
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--require-url", action="store_true")
    run.add_argument("--pack-workers", type=int, default=8,
                     help="Parallel shard writers for the packaging stage")
    run.add_argument("--stop-after", choices=("filter", "postprocess", "final-filter", "layer-filter", "detail-filter", "prepare-pack", "pack"),
                     default="pack", help="Stop after this completed stage; resume later with --resume")
    run.add_argument("--final-layout-overflow-policy", choices=("reject", "warn", "ignore"),
                     default="warn",
                     help="Layout overflow policy for the final filter (default warn)")
    add_viewports(run)
    add_rules_arguments(run)
    run.set_defaults(workers=None)
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
