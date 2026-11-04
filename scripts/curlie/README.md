# Curlie 按成功页数持续补采

直接使用 Curlie 人工分类目录作为候选池，不限定为预先选好的 2,000 URL。下载目录快照一次，建立本地 SQLite 索引，按类别持续领取候选并调用原有 TypeScript 采集器。不会逐批重新下载目录或抓取 Curlie 网页。

## 默认行为

- 目标 10,000 个**完整页面**，每页三个分辨率；因此目标约 30,000 张图片，不是 10,000 张。
- full 整页图，最大高度 8000 CSS px；每站探索 2–3 页，只从入口发现链接，沿用已有 URL/图片去重。
- 中央队列默认全局并发 12，每次预取最多 24 个候选。一个常驻 Node + Chromium，站点独立 context/page；完成一个就发布并补位，可跨多个候选分块并发，没有批次结束屏障。
- 成功页面较少的类别优先领取候选，失败、重复或只有少量有效页的站点不足以完成目标时，继续补站；不计尝试页数或不完整分辨率组。
- 域名按主机名归一化 www 后，在本次任务中只领取一次。不同子域、跳转后重复站点或模板不保证全局去重。原目录可能有多个类别，同站在本次任务中只归一个抽中的原始类别。
- 剩余名额限制批次大小；首页模式每站最多一页；开启探索时发现器最少两页，最后只剩一页时仅发布剩余的一页。活动任务预留页额度，避免全局并发超过目标。最终输出不超目标。
- 候选耗尽而目标未完成：状态 pool_exhausted、退出码 2，不伪装成成功。

当前目录快照已建池：13 类共 535,923 条 host/category 记录，508,983 个不同主机名。这些是候选，不是浏览器采集成功的网站数。只索引英文主题根类别，不将 World/Regional 等语言和地域分类重复加入。

## 运行

在 responsive-ui-dataset 下，先准备环境：

```bash
npm ci
npx playwright install --with-deps chromium
npm run build --silent
```

已有 examples/curlie-categorized/pool.sqlite 时直接用；没有数据库时，从原始目录压缩包自动建池：

```bash
mkdir -p examples/curlie-categorized
curl -fL --retry 2 https://curlie.org/directory-dl \
  -o examples/curlie-categorized/curlie-rdf-all.tar.gz
```

启动：

```bash
nohup python3 -u scripts/curlie/cli.py \
  --output runs/curlie-full-10k \
  --target-pages 10000 \
  --batch-sites 24 \
  --concurrency 12 \
  --export-only \
  > curlie-full-10k.log 2>&1 &
```

恢复加 --resume；已完成站点不重新采集，旧版未完成分块可直接按站点继续。允许修改 concurrency、batch-sites、site-timeout-seconds 和 browser-restarts；采集内容设置保持一致。未成功的站点不反复占用队列，后续批次从池中领取其他候选。原采集进程非零退出属于进程错误：保存批次并停止，避免配置或浏览器故障烧完候选池；修复后 --resume。普通网站失败仍在原采集器内部隔离，不停止补站。

## 首页模式与并发

默认 `examples/curlie-stream-full.json` 中 `discovery.enabled=false`：每站只采入口 URL，不读取链接、不展开菜单、不做页面图片相似度筛选。仍然顺序采完同页的三个分辨率，并沿用几何检查、全页限高、层级标注和版本化发布。失败站点继续从候选池补充。

显式开启原有多页面探索：`--explore-pages --max-pages-per-site 3`；也可在模板中设置 `discovery.enabled=true`。`--no-explore-pages` 强制关闭。普通 Node collect 配置含 discovery 时同样支持此开关，省略 enabled 默认关闭；不含 discovery 的显式 pages 列表采集保持原行为。

增加并发不需要增加浏览器数量：所有站点共用一个 Chromium，每个活跃站点独立 context/page，同一页面的分辨率顺序切换。实际活动站点上限由 `--concurrency` 全局控制，预取分块大小不再限制全局并发。可先使用 `--concurrency 20 --batch-sites 40`；20 是运行参数而非已验证的机器安全容量，应依据内存和超时率调整。站点加载和完整截图成本依然存在。

更换探索模式时使用新 output；改变并发/预取/站点超时/浏览器重启上限可以直接在原目录 --resume，不用另建目录。新任务通过 `--skip-history 旧任务目录` 继承去重历史，避免重采。

## 参数

| 参数 | 默认 | 用途 |
|---|---|---|
| --output | 必填 | 本任务结果和可恢复索引目录 |
| --target-pages | 10000 | 最终完整页面目标，非图片数 |
| --batch-sites | 24 | 每次预取候选数量，保留为索引/配置分块，不形成调度屏障 |
| --concurrency | 12 | 全局活动站点/context 上限，跨分块持续补位 |
| --site-timeout-seconds | 180 | 单站总墙钟期限，超时只关闭本站 context 并释放槽位 |
| --browser-restarts | 2 | 本次命令最多自动重启故障浏览器/执行器次数；耗尽后保持任务 pending 并退出 |
| --explore-pages / --no-explore-pages | 默认关闭 | 开启子页面探索 / 每站只采入口页；CLI 覆盖 discovery.enabled |
| --max-pages-per-site | 3 | 仅开启探索时生效，每站最多 2–5 页 |
| --categories | 13 个英文主题类 | 空格分隔的 Curlie 原始根类别子集，例如 News Shopping Computers |
| --config | examples/curlie-stream-full.json | 全批共用的分辨率、截图、去重和发现设置；不需要 sources/pages |
| --pool | examples/curlie-categorized/pool.sqlite | 本地候选数据库；只读，不保存任务成败 |
| --archive | examples/curlie-categorized/curlie-rdf-all.tar.gz | 无 pool 时用于首次建池的原始目录 |
| --export-only | 默认开启 | 新批次采集端只写图片和 hierarchy，质量/计时留在索引；发布后删除 .work |
| --no-export-only | 关闭精简 | 额外保留 .work/ 里的完整诊断产物 |
| --skip-history | 空 | 一个或多个旧任务目录或 collection-index.json；跳过旧任务所有已领取站点（含失败/中断），www 域名归一 |
| --resume | 关闭 | 继续原任务；可调整并发/预取/站点期限/浏览器重启上限，其他采集设置必须一致 |

模板决定 full/initial，export-only 决定是否保留中间产物，两者独立。默认 full 模板不保存 initial/overlay。更换目录快照时应新建候选数据库及任务输出，不覆盖正在恢复的候选池。

## 产物与恢复

```text
runs/curlie-full-10k/
├── collection-index.json         # 页数/图片数/各类页数/网站结果、URL、分类路径、时间及文件定位
├── url-history.jsonl             # 持续追加的入口、探索访问、失败/重复、实际导出历史
├── configs/00001.json            # 每批实际生效配置，体积小，保留供恢复
├── versions/<rule-version>/pages/<page-id>/<viewport>/
│   ├── full.png                  # 训练图
│   └── hierarchy.json            # 标签，capture.image 指向 full.png
├── .work/<batch-id>/             # 当前批次临时完整产物，完成发布后删除
└── .publishing/                  # 导出事务临时目录
```

版本目录中的 `pages` 是训练文件目录，每个分辨率恰好两个文件。恢复元数据保留在根部，不能一起删。两文件选择逻辑复用根目录 export_pages.py::training_files，支持 full.png 和旧 initial.png，不重命名图片。

### URL 历史（默认开启）

url-history.jsonl 独立于 .work，不会被 export-only 清理。每行是一个事件：site_queued（领取入口）、site_status（运行/失败/完成与原因）、url_visit（探索的页面 URL）、page_exported（实际保留的训练页面及路径）。记录批次、项目 ID、原始分类与路径、事件记录时间；访问事件还记录请求/最终 URL、来源 URL、发现方法、开始时间、耗时、状态、重复对象、相似度、错误及各分辨率结果。

采集期间每秒读取一次现有原子 discovery-index 检查点，并在进程退出、中断、导出和删除中间产物前再次同步。不会额外访问网站；快速经过的 running 等瞬态可能不出现，已写入检查点的最终访问结果会保留。记录的是探索页面，不是图片、字体、分析服务等全部网络请求。

事件有稳定 event_id，恢复读取已有历史去重；追加后 flush/fsync，最后半行若被中断会修复。已经完成的任务再次 --resume 不重复追加旧记录。旧版 export-only 已清理的详细访问历史不能凭空恢复；升级恢复时可补已有索引中的入口和导出记录，此后的访问开始完整记录。

页面先以完整分辨率组采入临时目录，再导出图片/标签，以目录 rename 提交，之后写恢复索引，最后清理临时产物。中断发生在发布期间时，下一次会核对已发布标签与仍保留的源标签，避免重复计数；发生在写索引之后、清理之前时，恢复会补清理。已经导出的文件人为丢失时明确报错，不继续虚报页数。

这仍是规则生成的未人工确认标注，成功数量表示截图/标签流程完成，不等价于训练 GT 已核验。没有启动 1 万页真实采集。已用本地 Chromium 验证：首站失败自动补位、三个分辨率完整组、目标 3 页精确停止、只保留 9 对训练文件、清理临时产物、恢复不重采。

## 代码职责

- archive.py：从已有 prepare-curlie-urls.py 移出的 TSV 流式读取，共享复用。
- pool.py：完整目录转 SQLite 索引、按类别读取队列。
- runner.py：公平补站、数量目标、子进程及信号、恢复调度；调用原 dist/cli.js collect。
- storage.py：原子索引和两文件发布；复用 export_pages.py 的格式规则。
- history.py：独立的 URL 访问历史、检查点同步、恢复去重与半行修复。
- cli.py：参数与模板，默认开启 export-only。
- test_pipeline.py：本地目录样例与真实浏览器回归，无外网依赖。

## 来源

[Curlie 数据介绍](https://curlie.org/docs/en/rdf.html)、[目录许可及署名](https://curlie.org/docs/en/license.html)。目录数据为 CC BY 3.0，网站内容不包含在目录授权内。原始来源和快照记录见 examples/curlie-categorized/source.json。

With content from Curlie.org - the largest human-edited directory of the web. Contribute by submitting a website or becoming an editor.

2026-11-03 修复：Curlie 含签名 PDF 的候选曾在 loadDiscoveryState 初始化抛异常，错误地终止整批。现在入口复用 candidateUrl 的网页过滤，越界、凭证参数、资源文件和排除动作入口均按站点标记 failed / invalid_entry，不进入浏览器队列，不影响同批其他站点；无需修改池或历史配置即可 --resume。

## 按标注规则版本隔离新产物

新发布的完整页面写入 versions/<hierarchy.rule_version>/pages/<page-id>/<viewport>/。版本从实际标签读取，不根据启动时间或当前代码猜测；中断批次若保留了旧版标签，会按其实际版本保存。极少数跨分辨率规则不一致的页面放入 versions/mixed，并在索引 annotation_rules 中逐视口记录，不冒充新版。

旧的根 pages/ 不移动、不覆写；首次升级 --resume 只从已有标签补齐总索引的 annotation_rule_version、annotation_rules 字段。新旧共享原调度检查点，已领取的域名不会因为换版本目录重新采集，目标页数仍包括已完成页面。totals.annotation_versions 展示各版本页面数；URL 历史和 WebDataset 元数据也记录新发布样本的版本。

因此旧数据只需按需离线清理一次，新版生成时已过滤，无需再次统一清理。训练可组合旧清理快照和新增版本目录，按 page_id 排除快照与新增目录可能的交集。WebDataset 打包传总采集根目录可保留完整来源/分类；不要把某个版本子目录当成独立的采集 resume 根目录。

### 更换采集策略并跳过旧 URL

```bash
python3 -u scripts/curlie/cli.py \
  --output runs/curlie-homepage-10k \
  --skip-history runs/curlie-full-10k \
  --target-pages 10000 --batch-sites 40 --concurrency 20 --export-only
```

旧索引只读，不要求旧图片仍在原位置。导入的是已领取站点的 host 去重集合（含旧失败和中断），不是旧任务的完成状态、页数或候选池 offset。新任务的 target-pages 是新增页数，旧图仍保留原处。若总目标包含旧数据，应自行将 target-pages 设为还缺的页数。

去重快照写入新 collection-index.json 的 excluded_hosts，启动日志 history_exclusions 给出数量。后续相同命令加 --resume，复用快照，不重读旧索引。多份历史可依次传给 --skip-history；被导入任务继承过的排除集合也会传递。旧任务应先停止，之后追加到旧索引的站点不会自动进入已有快照。缺失或格式错误的索引显式失败。

采集精简、计时范围与旧任务恢复行为见 [CAPTURE_PERFORMANCE.md](../../docs/CAPTURE_PERFORMANCE.md)。

## 中央 worker 调度（默认）

```text
Python 中央队列（SQLite 候选池 / 历史去重 / 成功页额度）
  → 常驻 Node 执行器 → 一个 Chromium
  → 至多 concurrency 个独立站点 context/page
  → 本站结束 → 立即发布本站完整页并记录索引 → 立即补下一站
```

不再为每个候选分块启动一个 Node/Chromium，也不等分块内最慢站点结束才补下一块。旧的 configs/、batches 和 .work/<batch-id> 只是存储分块，仍可恢复；index.batches[].source_jobs 记录本站的 pending/running/completed、尝试、结果和错误。每个站点完成即 checkpoint 和发布，只有清理共同临时目录需要等该分块没有活动/待处理任务。原输出布局、URL 历史和版本化规则不变。

示例：停止原任务后，可以直接调整并发恢复，不需要新目录：

```bash
python3 -u scripts/curlie/cli.py \
  --output runs/curlie-homepage-10k \
  --skip-history runs/curlie-full-10k \
  --target-pages 10000 --batch-sites 40 --concurrency 20 \
  --site-timeout-seconds 180 --export-only --resume
```

只改并发不会修改已有 configs 的采集语义；执行器按全局槽位派发 collectSite，不采用旧分块 config.concurrency 再叠乘。旧分块里 .work 已成功但根索引还没发布的页面，会直接发布，不再打开浏览器重新采集。异常/中断保留活动站点为待恢复状态；正常失败及超时视为本站已处理，从池里补新站。

日志 stream_ready 标记本次唯一 Chromium；collection_progress 每个站点结束输出；scheduler_status 每 10 秒报告活动数、成功页数、目标和最长在途时间；site_timeout 明确报告本站超时。浏览器进程整体退出属于基础设施错误，立即暂停派发、保留任务并有限重启（默认每次命令 2 次）；超过上限停止，避免烧完候选池。

实现：scheduler.py 中央队列，worker_process.py JSONL 传输与进程清理，src/application/discovery/stream-worker.ts 常驻浏览器执行器。复用原 collectSite/captureViewport。验证：test_scheduler.py 阻塞首站，证明下一分块先于首站完成开始；验证单站期限、共享单浏览器、全局并发上限、精确配额、运行中中断及旧格式恢复；test_pipeline.py 保留原回归。

浏览器退出、旧误判任务修复及验证边界见 [BROWSER_RECOVERY.md](../../docs/BROWSER_RECOVERY.md)。更新后启动日志应有 `executorVersion: browser_recovery_v1`。
