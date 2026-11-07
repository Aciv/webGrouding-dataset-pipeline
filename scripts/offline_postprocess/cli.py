"""Configuration-driven offline component processing and repeatable statistics."""
import argparse
import json
from pathlib import Path
from .config import load_config
from .application import run_dataset
from .statistics import compute_statistics
from .slicing_statistics import compute_slicing_statistics


def main():
    """Run offline processing or statistics locally; no network/model clients are initialized."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--input', required=True, type=Path)
    run.add_argument('--output', required=True, type=Path)
    run.add_argument('--config', type=Path)
    run.add_argument('--resume', action='store_true')
    run.add_argument('--limit-pages', type=int, default=0)
    run.add_argument('--page-ids', nargs='+')
    run.add_argument('--baseline', type=Path)
    stats = sub.add_parser('stats')
    stats.add_argument('--output', required=True, type=Path)
    stats.add_argument('--workers', type=int, default=12)
    stats.add_argument('--baseline', type=Path)
    slicing_stats = sub.add_parser('slice-stats')
    slicing_stats.add_argument('--output', required=True, type=Path)
    slicing_stats.add_argument('--baseline', type=Path)
    validation = sub.add_parser('validate')
    validation.add_argument('--output', required=True, type=Path)
    validation.add_argument('--workers', type=int, default=12)
    preview = sub.add_parser('preview')
    preview.add_argument('--output', required=True, type=Path)
    preview.add_argument('--observations', required=True, nargs='+')
    preview.add_argument('--image', required=True, type=Path)
    gallery = sub.add_parser('gallery')
    gallery.add_argument('--output', required=True, type=Path, help='Processed dataset root')
    gallery.add_argument('--gallery-output', required=True, type=Path)
    gallery.add_argument('--count', type=int, default=32)
    gallery.add_argument('--seed', type=int, default=20261107)
    gallery.add_argument('--workers', type=int, default=4)
    gallery.add_argument('--archive', action=argparse.BooleanOptionalAction, default=True)
    gallery.add_argument('--selection-manifest', type=Path, help='Replay prior viewport IDs, preserving rejected cases and original numbering')
    viewer = sub.add_parser('viewer', help='Build one offline HTML for interactive random sampling of the entire dataset')
    viewer.add_argument('--index', required=True, type=Path)
    viewer.add_argument('--output', type=Path, default=Path('outputs/dataset-viewer/index.html'))
    viewer.add_argument('--workers', type=int, default=8)
    layers = sub.add_parser('layer-stats', help='Report original source layers and current detail-training layers separately')
    layers.add_argument('--index', required=True, type=Path)
    layers.add_argument('--output', type=Path)
    layers.add_argument('--workers', type=int, default=8)
    args = parser.parse_args()
    if args.command == 'layer-stats':
        from .layer_statistics import compute_layer_statistics
        report = compute_layer_statistics(args.index, args.output, args.workers)
        print(json.dumps({'layers':report['layers'], 'totals':report['totals']},ensure_ascii=False))
    elif args.command == 'viewer':
        from .dataset_viewer import build_dataset_viewer
        report = build_dataset_viewer(args.index, args.output, args.workers)
        print(json.dumps(report, ensure_ascii=False))
    elif args.command == 'run':
        config = load_config(args.config)
        if args.limit_pages < 0:
            parser.error('--limit-pages must be nonnegative')
        summary = run_dataset(args.input, args.output, config, args.resume, args.limit_pages, args.page_ids)
        compute_statistics(args.output, config['workers'])
        compute_slicing_statistics(args.output, args.baseline)
        print(json.dumps(summary, ensure_ascii=False))
    elif args.command == 'slice-stats':
        summary = compute_slicing_statistics(args.output, args.baseline)
        print(json.dumps({'counts': summary['counts'], 'geometry_violations': summary['geometry_violations']}, ensure_ascii=False))
    elif args.command == 'preview':
        from .review import render_contact_sheet
        render_contact_sheet(args.output, args.observations, args.image)
        print(str(args.image))
    elif args.command == 'gallery':
        from .sample_gallery import generate_gallery
        summary = generate_gallery(args.output, args.gallery_output, args.count, args.seed, args.workers, args.archive, args.selection_manifest)
        print(json.dumps(summary, ensure_ascii=False))
    elif args.command == 'validate':
        from .validate import validate_dataset
        if not 1 <= args.workers <= 64:
            parser.error('--workers must be 1..64')
        report = validate_dataset(args.output, args.workers)
        print(json.dumps(report, ensure_ascii=False))
        if not report['passed']:
            raise SystemExit(1)
    else:
        if not 1 <= args.workers <= 64:
            parser.error('--workers must be 1..64')
        summary = compute_statistics(args.output, args.workers)
        compute_slicing_statistics(args.output, args.baseline)
        print(json.dumps({'observations': summary['observations'], 'totals': summary['totals']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
