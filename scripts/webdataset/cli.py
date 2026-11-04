#!/usr/bin/env python3
"""Package collected full/initial images and hierarchy labels into HF-compatible TAR shards."""
import argparse
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from writer import pack_dataset


def main():
    """Parse local packaging options; this command never uploads data to the Hub."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="采集根目录，保留索引才能携带 URL/分类")
    parser.add_argument("--output", type=Path, required=True, help="独立的空输出目录")
    parser.add_argument("--shard-size-mb", type=float, default=512, help="每个 TAR 的目标大小 MiB；单样本可超限")
    parser.add_argument("--samples-per-shard", type=int, default=1000, help="每个 TAR 最多截图样本数")
    parser.add_argument("--seed", type=int, default=42, help="打乱样本顺序的固定种子，不生成训练/验证划分")
    parser.add_argument("--require-url", action="store_true", help="任何样本缺少网页 URL 时失败")
    args = parser.parse_args()
    try:
        result = pack_dataset(args.input, args.output, args.shard_size_mb,
                              args.samples_per_shard, args.seed, args.require_url)
    except (ValueError, OSError) as error:
        parser.exit(1, f"打包失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
