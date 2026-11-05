#!/usr/bin/env python3
"""Package a collected dataset into URL-indexed, ~1 GiB compressed shards for Hugging Face."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pack import pack_dataset, pack_index


def main():
    """Parse packaging options; this command never uploads data to the Hub."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", dest="inputs", type=Path, nargs="+", action="append",
                        help="一个或多个采集根目录；可写 --input A B，也可重复 --input")
    source.add_argument("--index", type=Path,
                        help="pipeline 索引；按索引解析原图、派生 hierarchy、URL 和分类")
    parser.add_argument("--output", type=Path, required=True, help="独立的空输出目录")
    parser.add_argument("--chunk-size-mb", type=float, default=1024,
                        help="每个分块压缩后的目标大小 MiB（默认 1024）")
    parser.add_argument("--dedupe-by", choices=("url", "site", "none"), default="url",
                        help="按规范化页面 URL（默认）、站点域名去重，或 none 关闭")
    parser.add_argument("--seed", type=int, default=42,
                        help="打乱页面样本的固定种子，不生成训练/验证划分")
    parser.add_argument("--require-url", action="store_true",
                        help="任何页面缺少来源网页 URL 时失败")
    parser.add_argument("--resume", action="store_true",
                        help="仅索引模式：复用索引和选项都相同的已完成分块")
    parser.add_argument("--workers", type=int, default=1,
                        help="并行 shard 写入数；>1 时文件名变为 train-NN-xxxxx")
    args = parser.parse_args()
    if args.resume and args.index is None:
        parser.error("--resume 只支持 --index 模式")
    try:
        if args.index is not None:
            result = pack_index(args.index, args.output, args.chunk_size_mb,
                                args.seed, args.require_url, args.dedupe_by,
                                resume=args.resume, progress=True, workers=args.workers)
        else:
            inputs = [path for group in args.inputs for path in group]
            result = pack_dataset(inputs, args.output, args.chunk_size_mb,
                                  args.seed, args.require_url, args.dedupe_by,
                                  progress=True, workers=args.workers)
    except (ValueError, OSError, FileNotFoundError) as error:
        parser.exit(1, f"打包失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
