# 当前数据集 Pipeline

## 总体运行与分阶段停止

`run` 默认使用当前组件处理引擎，顺序为：
`filter → postprocess → final-filter → layer-filter → detail-filter → prepare-pack → pack`。
默认跑到压缩包，`--stop-after` 可取上述任一阶段。

```bash
# 先完成筛选与后处理，停在裁片后筛之前
python3 scripts/pipeline/cli.py run \
  --input datasets/curlie-full-10k \
  --workdir outputs/my-components \
  --config scripts/offline_postprocess/config.yaml \
  --workers 12 --pixel-workers 4 --pack-workers 8 \
  --stop-after postprocess

# 继续到最终压缩包
python3 scripts/pipeline/cli.py run \
  --input datasets/curlie-full-10k \
  --workdir outputs/my-components \
  --config scripts/offline_postprocess/config.yaml \
  --workers 12 --pixel-workers 4 --pack-workers 8 \
  --stop-after pack --resume
```

| 停止点 | 已完成内容 |
|---|---|
| `filter` | 原始质量、范围、像素预筛；不清框、不切片 |
| `postprocess` | 清框、分组、切片候选；不执行裁片后筛，不发布 trainable 数据 |
| `final-filter` | 裁片后筛、统计、独立校验和最终索引 |
| `layer-filter` | 融合与切片后只保留真实L1–L5，删除深层节点和裁片引用，不提升层级 |
| `detail-filter` | 按目标框并集覆盖率标记细节训练资格，不改变视口质量或整页切线监督 |
| `prepare-pack` | 通过视口的独立 PNG + 精简 JSON、URL 和打包索引 |
| `pack` | tar.gz 分片、metadata.jsonl 和 manifest.json |

同阶段的匹配检查点会复用，继续到后续处理阶段时重新检查来源并生成所需阶段结果。
输入目录、所选页面、规则版本、YAML 和工作并发参数必须一致；源文件变化会使缓存失效。
已完成的导出会验证文件哈希，已完成的压缩包按索引和压缩选项复用。
默认导出和压缩目录分别为 workdir 的兄弟目录 `<workdir>-pack-input`、`<workdir>-chunks`，
变化后自动选择 `-2` 等新目录，已有成品不覆盖。可用 `--prepare-output`、`--pack-output` 指定。
当前组件引擎接收一个输入根，筛选参数使用 YAML；旧 VLM/多输入模式须显式 `--engine legacy`。
`dataset` 支持这七个停止点，默认停在 `detail-filter`，得到已限L5并重算细节资格的最终索引。`final-filter` 是未截层的生成检查点，不可直接当成最终训练标注。

### 细节裁片训练选择

`run` 默认 `--min-detail-coverage 0.2`。所有可训练目标框（跨层）的并集面积占实际裁片
面积低于20%时，仅该裁片退出细节训练。重叠不重复计数，ignore与已剔除包装框不计入。
恰好20%保留；`--min-detail-coverage 0`仅关闭覆盖率门槛，像素有效性检测仍然生效。
低覆盖率不改变整页accepted状态、其他裁片、原图或切线标签；即使没有可用细节裁片，
视口仍保留供整页任务使用。严重页面质量检查和空白裁片拒绝仍按原有质量策略执行。

L5投影默认写在 `<workdir>-final-l5/`（可用 `--layer-output` 指定）；覆盖率选择结果默认写在 `<workdir>-detail-selection/`，可用 `--detail-output` 指定。
读取该目录的 `index-final.json`：`detection_crops` 仅引用可训练裁片，
`detail_crop_manifest` 包含所有原合格裁片及其标记，`discarded_detail_crops`单列被抛弃裁片。
原始生成索引/对象图/整页切线文件不改写，新的选择索引引用其独立几何校验报告。

成品每页JSON的 `crops` 保留全部候选及 `target_union_coverage`、
`detail_training_eligible`、`detail_training_discarded`、`detail_training_reason_codes`。
细节训练只读取 `detail_training_eligible=true` 或 `detail_training_crop_ids`；
`discarded_detail_crop_ids`用于审计，不能把“细节训练抛弃”理解为“整页质量失败”。
整页任务继续读取 `split_label` 和 `split_supervision_eligible`。

当前v4恢复历史横向严重溢出的拒绝策略：≥5个问题框且占比≥20%，
问题layout须横向超2px、丢失宽度≥10%。保持逐视口筛选与少框可保留，
仍执行空白裁片后筛。v3的warn策略产物仅作历史基线；版本与配置已变，不能续用旧缓存。

当前入口将 `page_filter` 的处理前预筛与裁片后质量检查接到同一条无VLM流水线：

```bash
python3 scripts/pipeline/cli.py dataset \
  --input datasets/curlie-full-10k \
  --workdir outputs/postprocess-components-v4 \
  --config scripts/offline_postprocess/config.yaml \
  --baseline outputs/postprocess-components-v3 \
  --workers 12 --pixel-workers 4
```

顺序为任务范围/视口预筛 → 框整理 → 局部分解 → 长图切片 → 裁片像素及几何质量检查 →
统计和独立校验 → `index-final.json`。一个YAML贯穿所有阶段；`--resume` 要求来源、版本和配置一致。

- `index-filtered.json`：逐视口预筛状态和原始文件指纹，正常少框页面不因旧min_boxes=10被误删。
- `index-postprocessed.json`：派生对象图指纹、处理状态及裁片候选数。
- `index-final.json`：校验通过后才发布，记录每视口最终状态和检测/切线资格，引用已通过的清单。
- `reports/filter-summary.json` / `.md`、`blank-crops.jsonl`：前后两阶段原因、触发裁片和被连带剔除的全部裁片数量。

任一实际裁片或核心区域命中空白/纯色，整个视口及其全部检测裁片、切线标签退出发布，
原始文件和失败裁片证据保留；同页其他正常视口仍独立保留。这是用户确认的严格策略。
第一阶段记录几何/layout代理证据，**不声称验证了CSS responsiveness**，未知仍为unknown。

当前中间索引schema为 `gui_component_dataset_index_v1`，坐标为原图CSS xyxy；先导出精简打包输入，不直接交给旧五层加载器。

### 最终打包输入

```bash
python3 scripts/pipeline/cli.py project-layers \
  --index outputs/postprocess-components-v4/index-final.json \
  --output outputs/postprocess-components-v4-final-l5 --workers 8

python3 scripts/pipeline/cli.py select-detail-crops \
  --index outputs/postprocess-components-v4-final-l5/index-final.json \
  --output outputs/detail-selection-components-v4-l5-20 --min-coverage 0.2

python3 scripts/pipeline/cli.py prepare-pack \
  --index outputs/detail-selection-components-v4-l5-20/index-final.json \
  --output outputs/pack-input-components-v4-l5-detail20 --workers 8
```

`pages/<page>/<viewport>/` 只包含原始 `full.png` 和精简 `hierarchy.json`。
JSON 包含 URL、对象图、可用裁片的 targets/ignore_regions、符合资格的切线标签；
不包含操作日志、像素诊断、源 DOM selector 或原始绝对路径。裁片图片按 `crop_bbox` 获取。
整页对象坐标和裁片局部坐标均为 xyxy；不可把裁片局部框直接画到整页。
拒绝和待审视口不导出，同页其余通过的视口保留。原始 PNG 按字节复制并校验 SHA-256。
输出必须是独立空目录，`pack-index.json` 最后发布，整个目录可以移动后再打包。

后续打包直接使用下列输入（`--dedupe-by none` 保持本次筛选结果数量）：

```bash
python3 scripts/pipeline/cli.py pack \
  --index outputs/pack-input-components-v4-l5-detail20/pack-index.json \
  --output outputs/chunks-components-v4-l5-detail20 \
  --chunk-size-mb 1024 --dedupe-by none --require-url --workers 8
```

`pack` 识别 `gui_component_pack_input_v1` 标签契约，不对新标签再做旧版层深筛选；
同页已接受的视口在同一分片、同一条 metadata 记录中。单独 `prepare-pack` 不压缩；`run` 默认继续压缩，不执行上传。
下游检测训练器接入和切线模型训练仍是独立工作。

---

以下是历史v13流水线说明；`filter/postprocess/final-filter` 与 `run --engine legacy` 保留用于旧数据兼容，
不属于当前组件/裁片数据入口，v13结果已废弃。

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
python3 scripts/pipeline/cli.py run --engine legacy \
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
python3 scripts/pipeline/cli.py run --engine legacy \
  --input runs/a runs/b \
  --workdir runs/pipe-run \
  --config scripts/layer_postprocess/config.local.json \
  --limit 100 \
  --stop-after postprocess

# 之后继续 final-filter + pack；已完成的阶段自动跳过
python3 scripts/pipeline/cli.py run --engine legacy \
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
python3 scripts/pipeline/cli.py run --engine legacy \
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
python3 scripts/pipeline/cli.py run --engine legacy \
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

### 裁片内纯色框检测

`detail-filter` 同时读取原始截图，对最终 L1–L5 正样本框逐框取 RGB 像素。
每个通道的最大值与最小值之差均不超过4（8-bit RGB）视作纯色/近纯色；
纯色框数量占该裁片全部正样本框数量 **≥60%** 时，仅标记该裁片无效。
重叠框按框分别计数；ignore不计数；没有正样本框时该检测不作判断。
这是一条像素启发式规则，不证明内容未加载或不可见，也不检测所有噪声。

输出保留裁片几何 `status`，增加 `crop_pixel_valid`、`box_pixel_evidence`
（分母、纯色数量、比例及对应框ID），训练资格为覆盖率与像素检查同时通过。
失败原因是 `mostly_uninformative_target_boxes`；训练manifest与打包JSON均携带标记。
该规则不连带拒绝其他裁片、整页或切线监督。旧结果不覆盖；默认pipeline缓存按规则版本区分。
