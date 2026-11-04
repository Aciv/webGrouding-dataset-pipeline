# Responsive UI DOM Dataset

文档入口：[docs 文档索引](docs/README.md)，按用途说明每份文件，并区分当前使用、规则草案、历史报告和研究计划。

Curlie 按成功页数持续采集：见 [调度器说明](scripts/curlie/README.md)。从完整人工分类目录补候选，默认 10,000 个完整三分辨率页面、full 限高 8000px、网站并发 12；export-only 默认只保留 full.png + hierarchy.json，根索引负责恢复。无需先固定一批 2,000 URL。

Hugging Face 中转与预览格式：见 [WebDataset 打包](scripts/webdataset/README.md)。独立脚本将图片和 hierarchy 打成 TAR 分片，并附上网页 URL、入口、分类、分辨率与 HF 数据卡，不修改采集结果。需要按约 1 GiB 分块压缩、一个网页的三个分辨率保持同块、解压后恢复原始目录结构、合并多个 run 并按来源 URL 去重、并生成含索引与源 URL 的 `metadata.jsonl` 时，见 [HF 分块压缩](scripts/hf_chunks/README.md)。

2,000 站初训候选已配置为三分辨率首屏、每站 2–3 页、网站并发 12，分成 20 批：见 [批量采集与迁移指令](docs/RANKED_2000_COLLECTION.md)。候选与配置生成不代表采集成功或标签已审核。

从现有 View 系列迁移的独立采集骨架。采集首屏图、限高整页图、密集 DOM 几何和叠框预览，**没有冻结训练标签、生成 COCO、训练模型或推断跨视口功能等价**。

最简 DOM → bbox 结果直接看每个分辨率目录的 `bboxes.json` 与 `overlay.png`：遍历所有 CSS 可见且与整页截图范围相交的元素，记录框并在原图上画绿色框。**不按 tag 或是否可交互筛选，也不合并/去重嵌套框。** `bbox` 是截图内裁剪框，`dom_bbox` 是原始 `getBoundingClientRect()` 框，均为 `[x,y,w,h]`。采集时滚动位置回到 (0,0)，此时 viewport 与 document 的坐标原点一致；CSS 可见性不等于精确排除遮挡。

整页截图复用旧版 `primeFullPage`：在高度上限内滚动触发懒加载、检查页面高度稳定、等待图片解码，然后回到顶部采集。宽度保持所选 viewport，截图高度为 `min(documentHeight, maxHeight)`，默认 `maxHeight=8000`。超过部分直接截断，并记录 `capture.truncated`。首屏 `initial.png` 仍保留。

只采首屏时设置 `captureMode: "initial"`，并保持 `artifacts.initial: true`。该模式不做整页滚动、不生成 full.png；截图和 bbox/hierarchy 的范围统一为当前 viewport，`capture.image` 为 initial.png。原始 DOM 仍保留屏外节点供追溯，导出可见框裁剪到首屏；`capture.truncated` 表示下方仍有未采内容，不代表叶节点或子集合完整。`maxHeight` 不裁剪首屏高度；未指定 captureMode 时保持原整页行为。

50 站首屏试采：

```bash
npm run collect -- --input examples/url-initial-50.json --output runs/url-initial-50-01
```

该配置从 url-candidates-100.json 按类别轮询抽取 50 个入口（类别内 Python Random(42) 洗牌）：Administration 2、Commerce 10、Community 10、Content 8、Organization / Landing 10、Service / Tool 10。保持手机 390×844、桌面 1440×900、平板 834×1210；每站从主页发现 2–5 页，同页连续采三个首屏；来源并发 2。50 指入口候选站点数，不保证成功数，也不是总页面上限。关闭 PNG/SVG 叠框文件落盘，核验网页仍能按层动态绘框。失败原因在 discovery-index.json；中断后原命令加 --resume。

给已采集的批次补生成框图（无需重新访问项目）：

```bash
npm run boxes -- --dir runs/pilot-20
```

## 安装、采集和查看

```bash
cd responsive-ui-dataset
npm install
npx playwright install chromium
npm test
npm run collect -- --input examples/pilot-20.json --output runs/pilot-20
npm run audit -- --dir runs/pilot-20
npm run review -- --dir runs/pilot-20 --port 4312
```

浏览器访问 `http://127.0.0.1:4312`。左侧选页面，上方切分辨率，可按 tag、直接文字、交互提示筛框，点击框/节点看原始属性、样式和父节点。

支持人工问题核验：选择已有框填写错误原因，或拖拽补框填写缺失类型和内容；可编辑、删除、刷新恢复。记录保存到各观测目录的 `human-review.json`，汇总保存为批次根目录的 `human-review-summary.json`。详见 [人工核验说明](docs/HUMAN_REVIEW.md)。

支持视觉层级候选：每个保留节点记录 `layer` 和规范化 `parent_id`，每层复选框支持任意多选、全选/全不选，也可对照原始 DOM 全部框，点击后查看/选择父节点。已有三批样本已生成；规则与实测见 [分层标注说明](docs/VISUAL_LAYERS.md)。离线重新生成：`npm run layers -- --dir runs/real-url-10`。

可见框还会应用内部滚动容器的祖先 overflow 裁剪，排除未滚出的内容并裁剪部分可见框。原始 DOM 保留；修正依据与限制见 [不可见框问题修正](docs/OVERFLOW_CLIPPING_FIX.md)。

裁剪后的面积 `width × height < 10 CSS px²` 直接排除，等于 10 保留。视觉层级模式额外修正图片链接行盒、列表内容范围，过滤装饰/广告和文字残片并记录 ignore 区域；原始 DOM 模式继续供对照。最深层文字策略未修改。每项条件、边界、历史优化和验证见 [统一优化记录](docs/OPTIMIZATION_LOG.md)。

共享可见性规则还识别确定为空的 CSS clip/clip-path、filter opacity(0) 和具有 CSS 证据的 sr-only 模式，包括祖先隐藏；可见细线和 aria-hidden 图标不会仅因此被删除。新采集保存相关样式，旧快照缺失字段保持未知。

DOM tag→struct 已独立在 [src/domain/structure](src/domain/structure/README.md)，由 collect、离线 layers 和核验验证共用。九类为 `text/image/graphic/media/control/table/list/region/block`，保留原始 tag 与 subtype。`block` 是有可见证据的通用块；证据不足归 `unknown + review`，纯包装排除。网页视觉模式新增 struct 与用途筛选，结果保存在 hierarchy.json；详细定义见 [结构标签草案](docs/STRUCT_LABEL_SPEC_DRAFT.md)，实际验证见 [STRUCT_VALIDATION.json](docs/STRUCT_VALIDATION.json)。

第一批使用仓库已存在的 5 个静态项目各 4 页，共 20 页 × 3 个视口：390×844、1440×900、834×1210。配置中的 source.root 相对于配置文件解析。工程搬到别处时需要同步提供这些项目目录或修改 root；采集器不会下载项目。部分源页面仍引用外部字体/CDN/图片，失败记录在 quality/network 中。Cara 许可证未核验，仅作内部测试材料。

URL 项目配置例：

```json
{
  "viewports": [{ "id": "desktop", "width": 1440, "height": 900 }],
  "sources": [{ "id": "site", "kind": "url", "baseUrl": "https://example.com/" }],
  "pages": [{ "id": "home", "source": "site", "path": "/" }]
}
```

动态本地项目先按原方式运行，然后把 baseUrl 指向其地址；static 只用于已构建静态目录，不会自动运行 pnpm/npm 安装与构建。

## 参数与配置

### 100 个真实 URL：仅主页发现子页

已将 `view-collector/examples/url-candidates-100.json` 的 100 个入口 URL 按原顺序迁入 [examples/url-candidates-100.json](examples/url-candidates-100.json)，保留 ID、分类和站点名。不需要携带旧 View 工程或旧采集结果。

```bash
npm ci
npx playwright install --with-deps chromium
npm run build
nohup node dist/cli.js collect --input examples/url-candidates-100.json --output runs/url-100-01 > url-100-01.log 2>&1 &
tail -f url-100-01.log
```

中断后使用同一配置和输出目录，追加 `--resume`。完整来源跳过；未完成来源复用已选页面，重试失败 URL/分辨率并继续剩余候选。参考图未保存时，使用已有 full.png 顶部原尺寸区域恢复图片去重参考。

- 只在主页读取 href（包括隐藏菜单中已经存在的链接），必要时有限展开菜单；子页不再提供探索候选。
- 相同 origin、可选 source.pathPrefix 范围；URL 作为页面标识，忽略 fragment、跟踪参数及默认 index 别名。
- 默认比较 desktop 的首屏，复用旧流程的原尺寸像素一致率，单通道容差 32；相似度达到 0.95 即排除。不是语义相似度。
- 每来源保留 2–5 个页面（包括主页，若主页采集成功）；达到 5 个立即停止。无法获得 2 个时标记 partial/failed，不伪造或强行保留相似页面。
- 候选参考分辨率先采集到临时目录并做去重，同一个 page 顺序切换其余分辨率。全部配置尺寸采集完成且 URL 检查通过后，整组发布并计入选页名额；失败候选仅写入 discovery-index.json，不发布缺分辨率的页面。
- `batch-index.json` 是页面产物索引；`discovery-index.json` 记录每来源候选、URL 来源、菜单检查、实际 URL、相似度、重复对象、失败原因与选中页面 ID。
- `concurrency` 为来源并发，共用一个 Chromium；每来源一个 context/page，没有分辨率并行或子页递归。

可调 `discovery`：`minPages=2`、`maxPages=5`（上限 5）、`maxVisits=20`（每来源每次运行）、`maxCandidates=60`、`menuInteractions=4`、`similarityThreshold=0.95`、`viewport=desktop`。不提供 discovery 时保持原来的显式页面采集。

### 可选诊断图片

```json
"artifacts": {
  "initial": false,
  "overlayPng": false,
  "overlaySvg": false
}
```

控制 initial.png / overlay.png / overlay.svg，普通旧配置未提供时默认全开；100 URL 和 10 站多页配置默认全关。整页模式固定保存 full.png，首屏模式必须保存 initial.png，且不生成 full.png；DOM、bbox、hierarchy 均保留。SVG 和核验网页引用 capture.image；audit、boxes、layers 遵守这些设置，不把主动关闭的图片当作缺失或重新生成它们。整页模式的首屏去重可用内存图，不要求落盘 initial.png。

10 站多页快捷运行：`bash scripts/collect-real-url-10-multipage.sh`。已有单页重采脚本保持原配置，也可通过 `COLLECTION_CONFIG` 指定其他配置文件。

- `collect --input`：批次 JSON；`--output`：输出目录。
- `collect --resume`：相同有效配置下跳过已完成页面，重试未完成页；重试会重采该页所有视口，防止混用不同页面生命周期的节点 ID。
- `review --dir`：输出目录；`--port`：本机端口，默认 4312。
- `audit --dir`：检查输出文件、图片尺寸、父引用与身份命名空间，写入 summary.json，不重新采集。
- `viewports`：分辨率列表，数量不限，id 唯一。
- `concurrency`：同时处理的页面数，默认 2。始终共用一个 Chromium，每页独立 context；同页各分辨率依次采集。
- `navigationTimeoutMs`：入口 DOMContentLoaded 等待上限，默认 30000。
- `settleMs`：复用旧代码的稳定等待参数，默认 300。
- `maxNodes`：每张观测保存的节点上限，默认 20000；超出会显式标记 truncated。
- `maxHeight`：整页图最大高度，默认 8000 CSS px；也限制滚动加载范围。它不改变浏览器 viewport 高度。

URL 来源沿用旧在线采集的 bounded_online 准备：两轮限时滚动，页面高度不稳定与图片未完成记 warning，按实际准备过的高度截图；本地/static 来源仍使用严格准备。两种策略都检查截图尺寸和标注范围，详细准备信息写入 quality.json 的 preparation。

## 输出

```text
runs/pilot-20/
├── batch-index.json             配置、环境版本、每页/分辨率状态、耗时、文件目录
├── summary.json                 audit 生成：节点/tag 分布、完整性与网络问题
├── index.html                   离线内容核验页面（通过 review 服务打开）
└── pages/<page-id>/
    ├── network.json             失败请求与 HTTP 错误，最多 100 条
    └── <viewport-id>/
        ├── initial.png          必保存：原始视口截图
        ├── full.png             限高整页图，宽度等于所选 viewport
        ├── nodes.json           原始节点、CSS 框、文字行框、父关系与实例 ID
        ├── bboxes.json          所有可见 DOM 的最简 bbox 清单，不筛 tag
        ├── overlay.png          原图 + 全部可见 DOM 红框，直接打开即可
        ├── dom.html             对应主 frame DOM 序列化快照
        ├── overlay.svg          原图叠框，橙色交互提示、蓝色其余候选
        └── quality.json         稳定性、图片、字体与已知采集限制
```

`captured` 只表示所选分辨率文件采集完成，不代表标签准确或资源完整。整页加载准备失败则该观测失败；图片解码错误、测量稳定性问题仍有诊断记录，训练之前必须筛查。主页发现模式的 batch-index.json 只收录全部分辨率完成的页面；来源级 `partial` 表示完整页面数量未达到 minPages，具体失败候选和各尺寸诊断在 discovery-index.json，可 resume。显式页面采集模式仍保留原来的页面级 partial/failed 诊断。原始快照与 SVG 可能含重复包装框，这是有意保留的标签选择输入。

旧的 `runs/pilot-20` 是首屏采集结果，无法仅靠画框补成整页数据。整页版本需要重新采集到新目录，例如 `npm run collect -- --input examples/pilot-20.json --output runs/pilot-20-full`。`boxes` 会根据各快照记录的图像范围重新画框，查看器也会自动选择对应图片。

`nodeId` 在同页 resize 中保持；不同页面/重新载入不可比较。`documentId + nodeId` 才是实例键。此版本不判断换节点后的语义身份、遮挡比例或 Card 语义。

代码与复制来源见 [迁移说明](docs/MIGRATION.md)。第一批采集后的统计记录在 [pilot 报告](docs/PILOT_RESULTS.md)（执行后生成/更新）。

限高整页版实测见 [整页采集报告](docs/PILOT_FULL_RESULTS.md)。查看新结果：`npm run review -- --dir runs/pilot-20-full --port 4312`。

之前 URL 候选中的 10 个真实网站已采集到 `runs/real-url-10`，配置为 `examples/real-url-10.json`。详情见 [真实网页采集报告](docs/REAL_URL_10_RESULTS.md)，查看：`npm run review -- --dir runs/real-url-10 --port 4312`。
