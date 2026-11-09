# 离线内容块整理与长图切片

## 统一随机查看HTML

直接打开 `outputs/dataset-viewer/index.html`，默认从全部保留结果中随机抽5个不同页面，
显示整页/源框/处理后框及全部裁片，可在页面内继续随机抽样，不必重新导图。
支持质量状态、视口、细节抛弃状态筛选、ID/URL搜索、固定种子、名单导出和原像素放大。
切换到“全部处理结果”可复核拒绝与待审项；低覆盖裁片有独立训练抛弃标记。
工具栏“层级组合”可只勾选 L1–L5 中任意一层或任意组合：整页处理后框与裁片目标框
同步过滤并按层着色（L1绿、L2蓝、L3橙、L4紫、L5青）；随机抽样的候选池也限制为
至少包含所选层的视口。原始候选模式仍展示全部源框，不套用最终层过滤。

数据更新后重新生成：

```bash
python3 -m scripts.offline_postprocess.cli viewer \
  --index outputs/detail-selection-components-v4-l5-20/index-final.json \
  --output outputs/dataset-viewer/index.html --workers 8
```

HTML内置完整标注索引并按需解压，原PNG按相对路径引用；保持仓库目录结构即可直接双击，
不需要HTTP服务器、外部CDN或网络。将HTML单独搬离仓库不会携带原始图片。
真实Chromium验证见旁边的 `browser-verification.json`。

## 文件夹选择版查看器

直接打开 `scripts/offline_postprocess/dataset_viewer_picker.html`，不需要先生成HTML。
页面只读取浏览器中手动选择的文件夹（`webkitdirectory` 或拖放），不上传、不联网、不需要HTTP服务。
自动识别两种目录：

- `pack-input-*/`：含 `pack-index.json`，自包含 `pages/` 原图与最终 `hierarchy.json`。一个文件夹即可查看最终数据；pack-input 不保留原始 source 标注，原始候选模式会明确显示不可用。
- run 结果（含 validated `index-final.json`）：除结果文件夹外，还需要加入原始数据源文件夹（如 `datasets/curlie-full-10k`）以读取原图和原始 hierarchy。若索引里的 audit/hierarchy 指向另一个 run（例如 detail-selection 指向 final-l5），把那个 run 文件夹也加入即可。

可以多次选择文件夹，文件会合并；检测到多个索引时页面顶部提供选择。层级信息在后台读取，
期间可以先抽样，读取完成后层级组合过滤生效。文件缺失会逐项显示为读取失败并给出需要补选的目录，
不会用其他文件顶替。测试：

```bash
node scripts/offline_postprocess/tests/test_dataset_viewer_picker.mjs
```

## 每层统计

统一HTML顶部展开“每个layer的统计数据”，可看当前层的整页去重框、细节训练去重框、
裁片出现次数、低覆盖筛选独占退出框数；原始source_layer分布另列。
最终L1=derived_depth 0，按融合后的实际parent链只保留L1–L5，不提升深层对象，也不剥掉inactive结构祖先。原始层级仅供来源审计。

```bash
python3 -m scripts.offline_postprocess.cli layer-stats \
  --index outputs/detail-selection-components-v4-l5-20/index-final.json --workers 8
```

结果位于选择索引目录的 `reports/layer-statistics.json`、`.csv`、`.md`，
原始层级为 `source-layer-statistics.csv`。仅统计质量通过的视口，跨视口不做对象合并。
pipeline在detail-filter之后自动补齐此统计；生成查看器时也会按索引指纹检查并补齐。

当前规则版本为 `component_slices_v5`。本入口替代旧 VLM／五层后处理。旧 v13 后处理结果已废弃，作为输入时明确拒绝。
原始 DOM 候选标签也不是人工真值；所有输出标记 `rule_generated_unverified`。

v5沿用v4的横向layout严重溢出拦截（2px容差、10%宽度损失、至少5框且占比≥20%）。
仍按视口独立筛选，不因另一视口失败连带删除正常截图，不按少于10框一刀切。
已确认域名停放截图加入哈希隔离；没有引入通用OCR或语义分类器。

## 运行

在仓库根目录执行（依赖见 `requirements.txt`）：

```bash
python3 -m scripts.offline_postprocess.cli run \
  --input datasets/curlie-full-10k \
  --output outputs/postprocess-components-v4 \
  --config scripts/offline_postprocess/config.yaml
# 相同输入、配置与版本的续跑
python3 -m scripts.offline_postprocess.cli run \
  --input datasets/curlie-full-10k \
  --output outputs/postprocess-components-v4 \
  --config scripts/offline_postprocess/config.yaml --resume
# 单独重新统计，不重新处理图片
python3 -m scripts.offline_postprocess.cli stats \
  --output outputs/postprocess-components-v4 --workers 12
# 独立检查源文件哈希、对象关系、裁片覆盖和坐标
python3 -m scripts.offline_postprocess.cli validate \
  --output outputs/postprocess-components-v4 --workers 12
python3 -m unittest discover -s scripts/offline_postprocess/tests -v
```

`--limit-pages 10` 或 `--page-ids <page_id> ...` 用于试跑，始终选取页面的全部现有视口。
不同输入范围使用不同输出目录。默认 12 个任务线程、4 个解码槽，适配约 32GB 内存；
每张图解码像素预算 6000 万。全部流程本地运行，不初始化 VLM，不发网络请求。

## 处理协议

先按原页面排除联合证据确认的极端密集表格，再逐视口校验质量。
自底向上整理小框、内容代表和重复，再清理大包装和有源内容依据的大区域。
保留结构图、源语义及原父关系，检测只取 `detection_eligible=true`。
显著标题、按钮、输入控件和主图受规则保护；规则没有能力证明其视觉显著性或人工误删率。

H≤2700 保留整图；H>2700 用像素分隔与框布局的全局规划生成水平切线。
核心分区完整无重叠，实际裁图可以添加上下文，但高度保持 900–1800。
完整目标可能在上下文中重复；被裁片边界截断的目标保留为 partial 正样本，框为裁剪后的可见框；只有源 ignore 仍是 ignore。
384 容量冲突进入 review，不截断目标。短页不为凑高度拉伸。

## 产物与消费

细节训练另做裁片级选择：当前默认可训练目标框并集覆盖率低于20%的裁片只加训练抛弃标记，
其他裁片、视口质量、整页原图和切线监督保持不变。生成层的 `crops/trainable.jsonl` 表示几何合格，
实际细节训练请读 `select-detail-crops` 产生的选择索引或成品的 `detail_training_eligible=true` 裁片。
相关pipeline阶段为 `detail-filter`，详见 [pipeline说明](../pipeline/README.md)。

以下为处理工作目录。最终精简交付使用 `scripts/pipeline/cli.py prepare-pack`，
详见 [pipeline 输入整理](../pipeline/README.md)。导出后每视口仅有整页 PNG 和包含 URL、
对象图、可用裁片标注、有效切线的 JSON，另有根目录打包索引。

- `observations/<page>/<viewport>/hierarchy.json`：稳定整页对象图，bbox 为原图 CSS **xyxy**；
  `source_bbox_xywh`、`layout_bbox`、源 ignore 与源 decisions 保留原始协议。
- 同目录 `operations.jsonl`、`partitions.json`、`result.json`：来源指纹、框操作、质量和切线审计。
- `quality/index.jsonl`：accepted/rejected/review，缺失证据保留 unknown。
- `crops/manifest.jsonl`：accepted 视口的裁图候选；`crops/trainable.jsonl` 仅含可用裁图。
- `split_labels/manifest.jsonl`：整页最终线候选；`trainable.jsonl` 仅含符合资格的伪标签。
- `reports/summary.json`、`summary.md`、`observations.csv`、`crops.csv`、`iou-buckets.csv`：可重算统计。

默认不复制 PNG，按 `source_image` 和 `crop_bbox` 按需裁图；配置
`slicing.materialize_crops: true` 可生成实体 PNG。全局对象 ID 不做跨视口语义对齐；
`group_id` 仅表示原页面划分组，不是跨视口对象身份。训练/验证必须先按页面或站点划分。

**不要把新图直接交给旧 v13／按层深过滤的数据加载器。** 图中的原始字段为来源事实，
资格以 `detection_eligible` 为准，训练应读取裁图的 targets/ignore_regions。
匹配与损失消费方必须实现正样本优先、ignore 排除无目标惩罚；本仓库不包含下游检测训练器。
本次实现离线标签生成和数据交接，不训练切线网络。推理阶段不得调用带真实框的 `plan_cuts`。

## 统计口径与限制

IoU 是同一**可训练裁片内标签框之间**的几何重叠，不是预测准确率。
每个宽度桶分别统计全部无序框对（含 IoU=0）及每框最大 IoU；后者排除没有邻居的单框样本。
源框、整理后全局框、裁片框出现次数、去重后的发布目标分别统计，避免上下文重复计数。
抛弃分为任务范围排除、质量拒绝、待审、聚合吸收、细粒度噪声、大包装及超大目标。

无 OCR 或已确认错误模板库，无法完整识别验证码、错误页及截图/标签错位；
已知地图嫌疑视口明确待审。规则阈值来自特殊样本对照，仍需独立人工质量评估。
窗口回退及内容目标始终残缺的整页不默认进入主切线监督；核心线穿过对象但上下文保全完整框时可以保留。少量组件/包装框边界损失按显式预算容忍。
删难目标后的指标不能直接解释为检测能力提升。

当前真实运行见 [RUN_EVIDENCE_V4.md](RUN_EVIDENCE_V4.md)，v3历史运行见 [RUN_EVIDENCE_V3.md](RUN_EVIDENCE_V3.md)，历史10例对照见 [RUN_EVIDENCE_V2.md](RUN_EVIDENCE_V2.md)，v1基线保留在 [RUN_EVIDENCE.md](RUN_EVIDENCE.md)。

## v2 局部分解与边界容忍

`local_regions` 控制大容器内部的横向/纵向 DCGen 式像素分隔与源框间隙提议。
新框来自已有目标的紧致并集，空白区域不会产生检测GT；进入整页切片后锁定对象图。
普通580px卡片不会因为边界损失被强制拆成小块。

原图像素完全相同的已确认验证码/错误页按配置隔离；其未复核的其他视口待审，
不声称它们必然错误。来源图变化后也需重新复核。零散候选噪声允许存在，未知可见性仍为unknown。

默认容忍每视口至多16个、且不超过有效目标总数10%的组件/包装框缺少完整视图。
已有文字、控件、前景图片、图形及有源证据的正文代表优先完整保留；缺失时才有限重试切片。
这些资格是任务协议，不是人工测得的噪声无害阈值。

```bash
python3 -m scripts.offline_postprocess.cli slice-stats \
  --output outputs/postprocess-components-v4 \
  --baseline outputs/postprocess-components-v1
```

专项报告为 `reports/slicing-summary.json` / `.md`，另含逐视口、逐裁片、逐切线CSV及
`slicing-problems.json`。统计实际核心/裁图高度、重叠、计算开销、ignore并集覆盖、
正样本与ignore重叠、内容/组件目标损失，以及上下文挽救率。模型训练端仍必须实现正样本优先。

## 随机整图与裁片可视化

```bash
python3 -m scripts.offline_postprocess.cli gallery \
  --output outputs/postprocess-components-v4 \
  --gallery-output outputs/postprocess-components-v4/reports/random32-20261107 \
  --count 32 --seed 20261107 --workers 4
```

从有发布检测裁片的accepted视口中均匀无放回抽样，不为凑长图重抽短页。
输出离线 `index.html`、`overview.jpg`、逐样本 `comparison.jpg`、原分辨率整图/裁片加框JPG、
`manifest.json` 和相邻ZIP。图库可以切换源候选框、最终框、无框原图以及裁片ignore。
`verification.json` 检查导出数量和原生尺寸，渲染时校验全局/局部框坐标与源文件指纹。
失败样本明确报告，不用其他样本替换；所有图只供复核，不修改训练产物。

## v3统一filter/pipeline入口

当前规则版本为 `component_slices_v5`。推荐使用 `scripts/pipeline/cli.py dataset`，
详见 `scripts/pipeline/README.md`。它发布pre-filter、postprocessed、final三份索引，
其中最终索引必须通过独立校验。v1/v2运行记录仅作历史比较。

`blank_crop_filter` 检查原始RGB的实际裁片及core；任何一片命中即剔除整个视口所有裁片和切线标签。
阈值为最大通道跨度≤4的近纯色，或前景≤0.01%且边缘≤0.02%的近空背景；不以框数作为保留理由。
这是显式数据政策，不等于图像语义真值。色彩变化明确的低灰度方差内容保留。

```bash
python3 -m scripts.offline_postprocess.cli gallery \
  --output outputs/postprocess-components-v4 \
  --gallery-output outputs/postprocess-components-v4/reports/random32-replay-20261107 \
  --selection-manifest outputs/postprocess-components-v2/reports/random32-20261107/manifest.json \
  --count 32 --workers 4
```

该命令沿用原32例及编号，被剔除项仍作为灰框审计展示，不补抽替换；`published_crop_views` 才是发布数量。
