"""导出训练所需的两种文件；仅使用 Python 标准库。

运行：python export_pages.py 输入目录 输出目录
输入支持 pages 本身，也支持任意层级的包装目录（例如 1/pages/）。
输出统一为 输出目录/pages/项目/视口/{图片,hierarchy.json}。
图片优先使用 hierarchy.capture.image（full.png 或 initial.png）。
多个 pages 合并；相同相对路径视为冲突，报错而不覆盖。
"""

import argparse
import json
import os
from pathlib import Path
import shutil


def training_files(hierarchy):
    """Resolve the canonical screenshot without misnaming a full-page image as initial."""
    hierarchy = Path(hierarchy)
    label = json.loads(hierarchy.read_text())
    name = label.get("capture", {}).get("image", "initial.png")
    if name not in ("initial.png", "full.png"):
        raise ValueError(f"不支持的训练图片名称：{name}")
    image = hierarchy.with_name(name)
    if not image.is_file():
        raise FileNotFoundError(f"缺少 {name}：{hierarchy.parent}")
    return image, hierarchy


def find_pages(source):
    """找到 pages 后停止向下搜寻，避免重复处理其内部目录。"""
    if source.name == "pages":
        yield source
        return
    for directory, children, _ in os.walk(source, followlinks=False):
        children.sort()
        if "pages" in children:
            pages = Path(directory) / "pages"
            if not pages.is_symlink():
                yield pages
            children.remove("pages")


def export_pages(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError(f"输入目录不存在：{source}")
    # 防止导出物被再次发现，也避免覆盖源文件。
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("输入和输出目录必须互不包含，请使用独立的输出目录")
    copies, destinations = [], set()
    pairs, skipped, roots = 0, 0, 0
    for pages in find_pages(source):
        roots += 1
        for hierarchy in sorted(pages.rglob("hierarchy.json")):
            try:
                image, hierarchy = training_files(hierarchy)
            except FileNotFoundError as error:
                skipped += 1
                print(f"跳过（{error}）")
                continue
            for file in (image, hierarchy):
                destination = output / "pages" / file.relative_to(pages)
                if destination in destinations or destination.exists():
                    raise ValueError(f"输出路径冲突，不覆盖：{destination}")
                destinations.add(destination)
                copies.append((file, destination))
            pairs += 1
    if not pairs:
        raise ValueError(f"没有找到完整的图片与标签对（发现 {roots} 个 pages 目录）")
    # 所有路径检查通过后才复制；原图、标签均保持原样。
    for file, destination in copies:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, destination)
    print(f"完成：{roots} 个 pages，{pairs} 组，{len(copies)} 个文件，跳过 {skipped} 组")
    print(f"输出：{output / 'pages'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="搜索根目录，或 pages 目录本身")
    parser.add_argument("output", type=Path, help="独立的导出目录")
    args = parser.parse_args()
    try:
        export_pages(args.source, args.output)
    except (ValueError, OSError) as error:
        parser.exit(1, f"导出失败：{error}\n")


if __name__ == "__main__":
    main()
