# 首页采集吞吐测试

直接调用生产 `dist/cli.js collect`，不省略三分辨率、全页截图、DOM 标注和完整性检查。只关闭子页面探索。不使用原任务的状态，不覆盖旧数据。

```bash
npm run build --silent
python3 scripts/benchmark/homepages.py \
  --output runs/benchmark-homepages \
  --sites 10 --concurrencies 2 4 \
  --timeout-seconds 180 --max-rss-mb 1800
```

| 参数 | 含义 |
|---|---|
| `--output` | 必填，新建的测试目录，禁止覆盖 |
| `--collector` | 被测构建的 CLI，默认 dist/cli.js，可指定保存的旧 dist/cli.js 作基线 |
| `--export-only` / `--no-export-only` | 默认精简模式；旧构建可能忽略此新配置，需要通过实际文件验证 |
| `--sources` | URL 来源配置，默认 `examples/real-url-10.json` |
| `--template` | 截图和标注配置，默认 `examples/curlie-stream-full.json` |
| `--sites` | 来源列表前 N 个站点，默认 8；更多候选需提供更大的 sources 配置 |
| `--concurrencies` | 依次测试的站点并发数，默认 2、4，每轮独立 Chromium |
| `--timeout-seconds` | 每轮最长运行时间，默认 180 秒 |
| `--max-rss-mb` | Node 和浏览器进程树 RSS 总和停止阈值，默认 1800MB，0 关闭；共享内存重复计数，非实际唯一物理内存 |

输出 `report.json`，每轮保存实际 config、collector.log、完整 capture 产物。核心指标是**全部分辨率采集成功的页面数 / 本轮实际墙钟时间**，包括进程启动、网络等待和写盘；非 URL 尝试数、非图片张数。默认目标 5 页/秒，即三个分辨率时 15 张图片/秒。

相同 URL 多次访问的缓存、站点波动和样本选择会影响结果；8–10 个健康站点是小样本冒烟测试，不能代表 Curlie 全量成功率或长期吞吐。内存/超时中断的轮次由 stop_reason 标出，不可当作完整完成的并发对照。正式容量测试需增大候选数量，站点数至少覆盖并发数，预留宿主机内存。

优化版 report.json 同时汇总 capture_stage_ms（分辨率阶段总量）、visit_stage_ms（访问阶段总量）和 capture_counts。两套阶段有包含关系，不要相加；totalMs 也不要与其子阶段再相加。

## 同页 DOM 提取对照

```bash
node scripts/benchmark/dom-extraction.mjs \
  --output runs/dom-extraction-comparison --sites 4 --repeats 3
```

复用生产 extractDom 和冻结的测试旧实现，同一个 page 上交替执行，避免把网络加载差异算作 DOM 加速。参数 --sources 指向含 sources 的配置（默认 examples/real-url-10.json），--sites 前 N 站（默认 4），--repeats 交替对照次数（默认 3），--output 为全新目录。仅此测速不截图，完整三分辨率采集速度仍需 homepages.py 测量。这里的 cached 方法包含结构缓存与 JSON 传输优化。

## 发布产物离线验证

```bash
node scripts/benchmark/verify-published.mjs runs/real-curlie-20-browser-recovery
```

读取 collection-index.json 与已发布两文件训练对，复用 checkHierarchy；检查 PNG IDAT zlib 解压/扫描行长度和过滤类型、截图与标签尺寸、URL/viewport/documentId、bbox 和 parent/layer。输出 published-verification.json，完整性错误退出码 1。不会打开网页或改写图片/标签，也不把 HTTP 200 挑战页、资源警告或图像质量自动视为合格。
