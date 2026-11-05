# 索引式离线 Pipeline：filter → postprocess → final filter → chunks

四个阶段只通过一个版本化 JSON 索引交接。**原始采集目录只读**：filter、final filter 和打包都不改文件；postprocess 只在独立目录新增一版 clean hierarchy。截图、URL、入口和分类始终从原始 run 与原始索引解析。

```
原始 run(s) ──filter──▶ index-filtered.json ──postprocess──▶ index-postprocessed.json
                                      │                              │
                                      │                              ├─ derived hierarchies（只含标签）
                                      └──────── final-filter ◀───────┘
                                                     │
                                                     ▼
                                             index-final.json ──pack──▶ tar.gz + metadata.jsonl
```

## 运行

在 `responsive-ui-dataset` 目录：

```bash
# 1) 先筛，避免对无效页调用后处理模型。--input 支持多个原始目录。
python3 scripts/pipeline/cli.py filter \
  --input runs/curlie-full-10k runs/curlie-homepage-10k \
  --output runs/pipe/index-filtered.json \
  --viewports desktop mobile tablet \
  --workers 8
```

`--input` 可以给多个采集根、包含 `pages/` 的目录、`pages` 本身或带 `collection-index.json` 的 Curlie 根。存在顶层 Curlie 索引时按已发布索引读取，`.work` 等临时产物不参与。默认要求 desktop/mobile/tablet 三套齐全；分尺寸样本需显式 `--viewports desktop`（只检查一个尺寸不代表通过多分辨率筛选）。

```bash
# 2) 后处理：只处理 filter-valid 页面，派生 clean L1–L5 hierarchy；不复制截图。
python3 scripts/pipeline/cli.py postprocess \
  --index runs/pipe/index-filtered.json \
  --index-output runs/pipe/index-postprocessed.json \
  --hierarchy-root runs/pipe/hierarchies \
  --processing-output runs/pipe/processing \
  --config scripts/layer_postprocess/config.local.json \
  --limit 100 --concurrency 4
```

- 派生文件位置：`hierarchies/run-000/<page_directory>/<viewport>/hierarchy.json`。
- 原始 hierarchy、截图、原索引一律不改；`processing/` 只放规则报告、候选和审计。
- 任一 viewport 失败或未被结构接受，整页 `rejected`，不会混进后续打包。
- 同一份 filter 索引可以用不同配置/版本生成多棵派生树，索引决定打包用哪一版。`--resume` 复用同版本、同源文件且 audit 为 accepted 的派生结果。

```bash
# 3) 最终筛：用原图重新校验派生 hierarchy。
python3 scripts/pipeline/cli.py final-filter \
  --index runs/pipe/index-postprocessed.json \
  --output runs/pipe/index-final.json \
  --min-boxes 10 --workers 8
```

后处理会增删节点、改 bbox 和层级，第一步的 `valid` 不能直接代表最终标签仍然有效。final filter 要求每个必需 viewport 都有 accepted 的派生 hierarchy，并重新检查节点数、训练 bbox 越界、layout 溢出、PNG 尺寸和 hierarchy SHA-256。

final filter 的 layout 溢出默认是 **`warn`**：仍然记录 `layout_horizontal_outside` / `layout_warnings`，但不再因为 clean 标签的 layout 比例单独拒绝页面；训练 bbox 越界仍照常拒绝。可选：

```bash
--layout-overflow-policy warn      # 默认：记录警告，不拒绝
--layout-overflow-policy ignore    # 不记录警告
--layout-overflow-policy reject    # 旧行为：layout 溢出也参与拒绝
```

clean L1–L5 的节点数通常少于原始树（实测 100 个 clean 样本中 4 个少于 10 个节点），如果直接沿用初始阈值，请先用 final-filter 的独立阈值确认损失。`run` 的 final 阶段对应参数是 `--final-layout-overflow-policy`。

```bash
# 4) 打包：截图来自原始目录，hierarchy 来自索引选中的版本，URL/分类来自索引。
python3 scripts/pipeline/cli.py pack \
  --index runs/pipe/index-final.json \
  --output exports/pipe-chunks \
  --chunk-size-mb 1024 --dedupe-by url --require-url
```

也可以直接使用 `python3 scripts/hf_chunks/cli.py --index ... --output ...`，两种入口等价。

## 一步跑完

```bash
python3 scripts/pipeline/cli.py run \
  --input runs/curlie-full-10k runs/curlie-homepage-10k \
  --workdir runs/pipe-run \
  --config scripts/layer_postprocess/config.local.json \
  --viewports desktop mobile tablet \
  --limit 100 --concurrency 4 \
  --pack-output exports/pipe-chunks
```

`--workdir` 首次必须为空，依次写出 `index-filtered.json`、`index-postprocessed.json`、`index-final.json`、`hierarchies/`、`processing/` 和默认的 `chunks/`。`run` 对初始和最终过滤使用同一组阈值；需要不同最终阈值时请分开执行四个阶段。

`run` 可以停在指定阶段：

```bash
# 只做 filter + postprocess，然后退出
python3 scripts/pipeline/cli.py run \
  --input runs/a runs/b \
  --workdir runs/pipe-run \
  --config scripts/layer_postprocess/config.local.json \
  --limit 100 \
  --stop-after postprocess

# 之后继续 final-filter + pack；已完成的阶段自动跳过
python3 scripts/pipeline/cli.py run \
  --input runs/a runs/b \
  --workdir runs/pipe-run \
  --config scripts/layer_postprocess/config.local.json \
  --resume
```

`--stop-after` 可选 `filter`、`postprocess`、`final-filter`、`pack`（默认 `pack`）。停止时会打印 `pipeline_stopped` 和已完成索引路径。之后用同一个 `--workdir --resume` 继续；`--limit` 留下的 pending 页会在不带 limit 的 resume 中继续处理。

## 并发

上万页时三个阶段的并发参数是分开的：

| 阶段 | 参数 | 默认 | 说明 |
|---|---|---|---|
| 建索引 + filter / final-filter | `--workers` | 8 | 建索引读取/hash hierarchy、以及逐页几何检查都走线程池；I/O 为主，可调到 16–64 |
| postprocess | `--concurrency` | 取 `config.local.json` 的 `concurrency`，当前是 **1** | VLM 请求并发，范围 1–256；不受 `--workers` 影响。想快必须显式传，例如 `--concurrency 32`，同时注意 API 限流和内存 |
| postprocess 像素分析 | `config.pixels.pixel_concurrency` | 0（不单独限制） | 限制同时做 Pillow/NumPy 像素候选的 observation 数；VLM 可以 64 并发，但建议像素分析只开 4–8 |
| pack / chunks | `--workers`（`run` 中是 `--pack-workers`） | 1 | 多个 shard writer 并行 gzip；>1 时分片名变成 `train-00-xxxxx.tar.gz`，页仍然整页进同一个分片 |

8GB 机器建议从 `--workers 32 --concurrency 32` 开始；如果页面像素回退多，在 config 的 `pixels` 里加 `"pixel_concurrency": 6`，再逐步把 `--concurrency` 提到 64。VLM 并发上限已经从 32 放宽到 256，但 API 限流和内存仍是实际约束。

示例（10k 级）：

```bash
python3 scripts/pipeline/cli.py run \
  --input runs/a runs/b \
  --workdir runs/pipe-run \
  --config scripts/layer_postprocess/config.local.json \
  --workers 32 \
  --concurrency 32 \
  --pack-workers 8 \
  --pack-output exports/pipe-chunks
```

说明：

- `--workers` 同一值用于索引构建、初始 filter、final filter；它们都是本地 I/O/JSON 处理。
- `config.local.json` 里 `"concurrency": 1` 是保守默认，不会自动被 `--workers` 放大；VLM 并发必须用 `--concurrency` 单独指定。
- pack 的 `--workers` 只影响写入速度和分片命名，不影响页原子性；`metadata.jsonl` 的 `chunk` 字段会指向正确分片。
- filter 的线程数只影响本地速度，不会触发模型调用。

## 进度与恢复

各阶段会输出 JSON 进度行（stderr/stdout 中与结果一致的 JSON Lines）：

- `pipeline_index_progress`：构建初始索引时按 run 报告已发现/已读取的 hierarchy 数
- `pipeline_filter_progress`：filter / final filter 的 start、running、百分比、valid/rejected
- `postprocess_progress`：start、已处理 observation 数、accepted/failed 状态分布、耗时；每个 VLM 请求本身还会打印 `vlm_started` / `vlm_finished`
- `pack_progress`：start、已写入页数、分块数、耗时；复用完成包时打印 skipped
- `pipeline_stage`：`run` 中每个阶段 started / skipped / resuming / new_output

postprocess 每 30 秒会输出一次带 `in_flight` 的心跳，即使没有 observation 完成也能看出是网络等待还是本地处理。进程启动时注册了 `SIGUSR1`：卡住时执行 `kill -USR1 <pid>`，会向 stderr 打印所有 Python 线程栈，不需要 py-spy。

重复与恢复规则：

| 阶段 | 重复/恢复方式 |
|---|---|
| filter | 索引是原子写入，没有半成品。完成后可用 `--overwrite` 原地重跑；不支持按页续跑，通常几秒到几分钟，建议直接重跑。`run --resume` 会校验输入目录、viewport 和规则一致后跳过已完成索引。 |
| postprocess | **支持真正的续跑**。每个 observation 的 `fused-hierarchy.json` / `layer-review.json` / 派生 `hierarchy.json` 都是检查点；`--resume` 复用同版本、同源、audit 为 accepted 的结果，只重试 failed / 未完成的 observation，不会重复调用已成功的 VLM 请求。 |
| final-filter | 纯本地重算，无外部调用。`--overwrite` 原地重跑。`run --resume` 在父索引、viewport、规则都一致时跳过。 |
| pack | 完成的包带 `manifest.json`。`--resume` 仅在最终索引 SHA-256、chunk size、seed、dedupe 全部一致时复用；否则报错。`run --resume` 默认输出目录会自动寻找匹配的 `chunks` / `chunks-2` / …，避免覆盖旧包。 |

`run --resume` 的实际用法：

```bash
python3 scripts/pipeline/cli.py run \
  --input runs/a runs/b \
  --workdir runs/pipe-run \
  --config scripts/layer_postprocess/config.local.json \
  --resume
```

- 已完成阶段打印 `pipeline_stage ... skipped`，不重跑。
- postprocess 有 failed observation 时打印 `resuming`，只重试失败的 observation；之前已成功的 observation 直接复用，不再调用 VLM。
- 如果上次用 `--limit` 只处理了前 N 页，这次 `--resume` 不传 `--limit`（即 0）会重新进入 postprocess 处理剩余页；已成功的页仍然复用。
- 如果重试改变了最终索引，默认分块会写到新的 `chunks-2`（保留旧包），不会静默覆盖；显式传 `--pack-output` 时由你负责选择新目录。
- filter 阶段的输入目录、viewports 或规则与已有索引不一致时直接报错，避免把不同上下文拼在一起；需要改变这些参数请换一个 `--workdir`。

## 索引结构

```json
{
  "schema": "responsive_ui_pipeline_index_v1",
  "stage": "filtered | postprocessed | final",
  "parent": {"path": "...", "sha256": "..."},
  "required_viewports": ["desktop", "mobile", "tablet"],
  "runs": [{"id": "run-000", "root": "/abs/original/run"}],
  "hierarchies_root": "/abs/derived/hierarchies",
  "pages": [{
    "page_id": "...", "source_id": "...", "url": "...", "entry_url": "...",
    "category": "...", "run_id": "run-000", "page_directory": "pages/...",
    "status": "valid | rejected | pending_postprocess | pending_final_filter",
    "reasons": [],
    "viewports": {
      "desktop": {
        "image": "pages/.../desktop/full.png",
        "original_hierarchy": "pages/.../desktop/hierarchy.json",
        "hierarchy": "run-000/pages/.../desktop/hierarchy.json",
        "hierarchy_side": "original | derived",
        "hierarchy_version": "...",
        "hierarchy_sha256": "...",
        "filter": {"valid": true, "reasons": [], "bbox_count": 42},
        "postprocess": {"status": "accepted", "version": "..."}
      }
    }
  }]
}
```

- `image` 和 `original_hierarchy` 相对 `runs[].root`；`hierarchy` 在 `original` 侧也相对 run root，在 `derived` 侧相对 `hierarchies_root`。
- `parent` 记录上一阶段索引的文件路径和 SHA-256，阶段产物必须写到新文件，不覆盖已完成索引。
- URL/分类优先从原始 `collection-index.json` / `batch-index.json` 继承，缺失时回退 `hierarchy.capture.url` 和 `nodes.json`，绝不按目录名猜测。

## 打包产物

打包仍按原目录树命名成员：`<page>/<viewport>/full.png` 和 `<page>/<viewport>/hierarchy.json`；多个 run 时加 `run-000/` 前缀。区别只在 hierarchy 内容：来自 `index-final.json` 选中的派生版本。`metadata.jsonl` 的每个 viewport 额外记录：

- `hierarchy_source`: `original` 或 `derived`
- `derived_hierarchy_path`: 派生 hierarchy 相对 `hierarchies_root` 的路径
- `annotation_rule_version` 和 `hierarchy_sha256`

`manifest.json` 额外记录 `pipeline_index`（路径、SHA-256、阶段、派生根、必需 viewport、跳过的页数）和 `hierarchy_policy`。截图成员保留原始 PNG 字节，不做重编码。

## 保证与限制

- filter、final-filter、pack 不写原始 run、原索引或派生 hierarchy；postprocess 只新增派生目录。
- 每个页面是原子单位：必需 viewport 任一缺失或无效，整页不进入 final/pack。
- 单个页面或 viewport 失败不会终止其他页；失败原因写入索引。
- 不自动上传，不生成 train/val/test 划分，不声明人工真值。
- 打包只取 `required_viewports` 定义的尺寸；索引里额外发现的 viewport 会保留记录但不进包。
- postprocess 的索引模式需要真实处理，不提供 dry-run；预览仍可使用 `scripts/layer_postprocess/cli.py` 的原有命令。
- 原始 run 含大量页面时，索引中的截图不哈希，避免重复读全量大图；hierarchy 哈希在每次阶段切换时计算并校验。

## 验证

```bash
python3 -m unittest discover -s scripts/pipeline -p 'test_*.py' -v
```

测试覆盖多原始目录、整页拒绝、必需 viewport、图片尺寸、派生 hierarchy、最终筛、哈希漂移、索引打包和 CLI。原有 `page_filter`、`layer_postprocess`、`webdataset`、`hf_chunks` 的测试保持通过。
