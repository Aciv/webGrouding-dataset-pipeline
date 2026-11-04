# Hugging Face 分块压缩与 URL 索引

独立于采集的后处理：读取一个或多个采集 run 里已经发布的页面（`full.png`/`initial.png` + `hierarchy.json`），先把同一网页的所有分辨率归为一个逻辑页面样本，再按来源 URL 可选去重，最后按约 1 GiB 切成压缩分块并生成逐页面 `metadata.jsonl`。三分辨率（mobile/desktop/tablet）始终在同一个分块、同一行索引中，分块边界只出现在页面之间；**但包内成员保留原始相对路径、原文件名和原 hierarchy 字节，解压后恢复原来的文件树形状**。JSONL 每行包含零基 `index`、实际来源网页 `source_url`、入口 URL、分类、各视口尺寸、分块与成员路径、SHA-256。仅使用 Python 标准库，不打开浏览器、不重新编码 PNG、不改采集源文件，也不自动上传。

与 [WebDataset 打包](../webdataset/README.md) 的区别：那份脚本按单张截图输出未压缩 `.tar`，面向上传后的 Hub WebDataset 浏览/流式加载；本脚本按页面合并三分辨率，输出 gzip 压缩分块和逐页面索引，面向大体积数据的上传、下载和自定义训练读取。包内是按页面组织的 TAR，不是按同名 key 配对的 WebDataset 样本布局；需要 Hub 侧标准 WebDataset 时继续使用 `scripts/webdataset`。

## 打包

在 responsive-ui-dataset 目录下：

```bash
python3 scripts/hf_chunks/cli.py \
  --input runs/curlie-full-10k runs/curlie-homepage-10k \
  --output exports/merged-chunks \
  --chunk-size-mb 1024 \
  --dedupe-by url \
  --require-url
```

`--input` 可以给多个采集根目录（也可重复写 `--input A --input B`）；每个都必须是含 `collection-index.json` / `batch-index.json` 的根目录，不要只复制 `pages` 后丢掉索引。多个输入不能互相包含。页面发现复用根目录 `export_pages.py::find_pages`，URL/入口/分类复用 `scripts/webdataset/provenance.py`。缺少 URL 时依次尝试索引、`hierarchy.capture.url`、同目录 `nodes.json`，都没有则记为 null，绝不根据目录名猜 URL；`--require-url` 会把缺失变成失败。

| 参数 | 默认 | 作用 |
|---|---|---|
| --input | 必填 | 一个或多个含发布页面及来源索引的采集目录 |
| --output | 必填 | 独立的空输出目录；与所有输入不能互相包含，非空目录拒绝覆盖 |
| --chunk-size-mb | 1024 | 每块压缩后的目标 MiB（默认约 1 GiB） |
| --dedupe-by | url | `url` 按规范化页面 URL 去重；`site` 按站点域名去重；`none` 关闭 |
| --seed | 42 | 固定种子打乱页面样本顺序；不产生训练/验证划分 |
| --require-url | 关闭 | 任意页面缺少来源网页 URL 时拒绝打包 |

**三分辨率不拆分**：同一 run 内同一 `source_id + page_id` 的所有视口先组成一个页面样本，再整页写入分块；写完后才检查分块大小。因此即使目标分块很小，也不会把同一页的 mobile/desktop/tablet 分到两个块。单个页面样本大于目标时该分块会超过目标（通常只超出几 MB）。重复视口或同一页各视口 URL 冲突会显式报错，不静默合并。

## 多个 run 与来源 URL 去重

去重以**整页**为单位，不会把一个页面的三个分辨率拆开，也不会把不同 run 的视口拼成一个页面。重复页面的保留规则：

1. 优先保留视口更多的页面组（例如完整 3 个视口优先于只有 2 个）；
2. 视口数相同时保留输入顺序更早的 run（`--input A B` 中 A 优先）；
3. 同一 run 内再按页面 ID/路径稳定排序。

`--dedupe-by url`（默认）的规范化规则：

- `http` / `https` 视为同一页面；主机名小写并去掉开头的 `www.`、默认端口；
- 去掉 fragment，去掉 `utm_*`、`gclid`、`fbclid` 跟踪参数；
- `/index.html` / `/index.htm` 视为 `/`；
- 其余 query 参数按名称/值排序，但参数本身仍参与页面身份；
- 路径大小写和其余非跟踪 query 不会被忽略。

因此 `http://www.example.com/index.html?utm_source=a` 与 `https://example.com/` 会去重。`--dedupe-by site` 只按主机名去重，一个站点最终最多保留一个页面，范围更强，可能丢掉同站其他页面。`--dedupe-by none` 关闭跨页去重。

没有 URL 的页面无法按 URL/站点建立身份，会各自保留；配合 `--require-url` 则直接失败。所有被丢弃的页面写入 `duplicates.jsonl`，包含规范化 key、保留/丢弃的 run、page_id、URL、视口列表和原因；manifest 的 `dedupe` 与 `inputs` 给出汇总。脚本不会重新访问网站校验 URL 是否仍有效。

## 产物

```text
exports/merged-chunks/
├── README.md                # HF 数据卡
├── manifest.json            # 完成标记、输入 runs、去重统计、分块大小/SHA-256
├── metadata.jsonl           # 逐页面索引：index、source_url、run、chunk、三视口成员路径等
├── duplicates.jsonl         # 按 url/site 去重时生成：被丢弃页面及最终保留页面
└── data/
    ├── train-00000.tar.gz
    ├── train-00001.tar.gz
    └── ...
```

分块成员**保留采集发布时的原始相对路径、原文件名和原 `hierarchy.json` 字节**，不会改名成 `desktop.png`，也不会额外插入 `page.json`。同一页面的三个视口只保证相邻并落在同一个分块中：

```text
versions/<rule-version>/pages/<page-id>/desktop/full.png
versions/<rule-version>/pages/<page-id>/desktop/hierarchy.json
versions/<rule-version>/pages/<page-id>/mobile/full.png
versions/<rule-version>/pages/<page-id>/mobile/hierarchy.json
versions/<rule-version>/pages/<page-id>/tablet/full.png
versions/<rule-version>/pages/<page-id>/tablet/hierarchy.json
```

旧式根 `pages/<page-id>/<viewport>/...` 会按原路径保留。把同一 run 的所有分块解压到同一目录，就恢复原来的已发布训练文件树（每个视口仍是 `full.png`/`initial.png` + `hierarchy.json`，`capture.image` 不变）。分块只是把树切段，不改变树的形状。

单个输入 run 解压后直接得到原树；多个 run 时，每个 run 的树放在独立的 `run-000/`、`run-001/` 前缀下，避免相同相对路径互相覆盖。`manifest.json` 的 `inputs` 映射 run ID、目录名和页面数。

`metadata.jsonl` 每行一个页面，主要字段：

| 字段 | 含义 |
|---|---|
| index | 零基页面索引；仅用于 JSONL，不改变包内路径 |
| sample_id | 九位补零页面索引 |
| source_url / url | 实际截图网页 URL；`source_url` 为显式别名 |
| entry_url | 配置中的入口网页 URL |
| page_id / source_id | 发布页面 ID / 来源站点 ID |
| run_id / run_name | 来源 run 的稳定 ID（`run-000`…）/ 输入根目录名；完整映射见 manifest.inputs |
| title / category / category_path | 索引记录中的标题与 Curlie 原始分类 |
| url_source | URL 来自 `collection-index.json`、`batch-index.json`、`hierarchy.json` 或 `nodes.json` |
| annotation_rule_version | 页面标签规则版本；跨视口不一致时为 `mixed` |
| image_count | 本页包含的视口数（完整页通常为 3） |
| chunk | 分块相对路径，例如 `data/train-00000.tar.gz` |
| viewports | 视口映射；每个视口含 `image_member`、`hierarchy_member`、`width`、`height`、`captured_at`、`annotation_rule_version`、`image_bytes`、`image_sha256`、`hierarchy_bytes`、`hierarchy_sha256`、原始图片/标签路径 |

`viewports[<viewport>].image_member` / `hierarchy_member` 就是包内原始路径；单 run 时与 `original_image_path` / `original_hierarchy_path` 相同，多 run 时多一层 `run-<id>/` 前缀。`hierarchy_sha256` 对应原 `hierarchy.json` 文件字节，不是重新序列化后的 JSON。训练代码可以读 `metadata.jsonl` 定位成员，也可以直接把所有分块解压回原树后按原逻辑读取。

不支持在非空目录覆盖或续写。打包中断会留下 `.partial` 分块和 `metadata.jsonl.partial`，没有 `manifest.json`；请换新输出目录重新打包。源文件始终保留。

## 上传到 Hugging Face

脚本本身不联网、不上传。确认分块和 `metadata.jsonl` 后执行：

```bash
pip install -U huggingface_hub
hf auth login
hf repos create 你的用户名/responsive-ui-chunks --repo-type dataset --private --exist-ok
hf upload 你的用户名/responsive-ui-chunks exports/merged-chunks . --repo-type dataset
```

上传约 1 GiB 的文件可减少小文件请求；`hf upload` 会自动使用 LFS/Xet，不需要手动切成 LFS 片段。数据卡不声明 WebDataset `data_files`，避免 Hub 自动加载器把按页面组织的 `.tar.gz` 误当作可直接流式读取的 WebDataset；Hub 预览/标准流式加载请使用按单图组织的 `scripts/webdataset`。

## 下载与训练读取

```bash
hf download 你的用户名/responsive-ui-chunks --repo-type dataset --local-dir /data/responsive-ui-chunks
```

```python
import json, tarfile

rows = [json.loads(line) for line in open("metadata.jsonl", encoding="utf-8")]
row = rows[0]
with tarfile.open(row["chunk"], "r:*") as archive:
    for viewport, entry in row["viewports"].items():
        png = archive.extractfile(entry["image_member"]).read()
        hierarchy = json.load(archive.extractfile(entry["hierarchy_member"]))

print(row["index"], row["source_url"], row["viewports"].keys())
```

也可以先把所有分块解压到同一目录，直接得到原来的训练文件树：

```bash
mkdir -p restored
for shard in data/train-*.tar.gz; do tar -xzf "$shard" -C restored; done
```

单 run 时 `restored/` 内就是原 `pages/` 或 `versions/<rule>/pages/` 树；多 run 时在每个 `run-<id>/` 下恢复各自的树。分块解压顺序不影响结果。只包含已发布的训练图片与 `hierarchy.json`，不包含采集索引、日志、`nodes.json` 或叠框诊断文件。

同站点的不同页面应在后续划分中放在一起，避免泄漏。这里 `train` 只是分块前缀，没有替你划分数据集。

## 限制与验证

- 图像保持原始 PNG 字节；不压缩图片、不裁剪、不重采样。
- 三分辨率强行同块，因此单页体积可能让分块略超目标；不会为了卡目标拆页。
- 默认 URL 去重会把 http/https、`www`、跟踪参数等变体视为同一页面；不能接受这种归并时用 `--dedupe-by none`。
- 去重只保留一个完整页面组，不会跨 run 合并视口，也不会判断页面内容是否相同。
- hierarchy 仍是规则生成的未人工确认标注，不因打包成为人工 GT。
- gzip 分块不支持随机访问；顺序解压或按整块读取。
- 包内保留原始训练文件树，不生成按索引重命名的 `page.json`；三个视口只保证在同一分块和同一 JSONL 行。
- 包内不是按同名 key 配对的 WebDataset 样本；需要标准 WebDataset 时用 `scripts/webdataset`。
- 网页截图权利与目录元数据许可分开；脚本不自动声明统一许可证。
- 模块：`cli.py` 参数；`pack.py` 发现、页面合并、索引、分块；`test_packaging.py` 标准库测试。
- 本地测试：`python3 scripts/hf_chunks/test_packaging.py`。
- 已用 `exports/wordpress-hierarchy-refined` 的真实导出验证：1 页 3 视口打为 gzip 分块后解压，恢复出与源目录完全一致的 6 个文件，`full.png` 与 `hierarchy.json` 字节逐一相同。

参考：[HF WebDataset 格式](https://huggingface.co/docs/hub/datasets-webdataset)。尚未用真实 10,000 页产物上传 Hub；1 GiB 的默认值来自本仓库已有采集规模，不声称某个仓库的在线预览已通过。
