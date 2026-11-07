# component_slices_v4 实际运行记录

## 恢复范围

按用户要求恢复 Git `9f62b6c` 的横向严重溢出拦截。原 `inspect_geometry` 未修改：
2px 容差、横向丢失宽度至少10%、至少5个问题框且占比至少20%时拒绝该视口。
仍保持 min_boxes=0、逐视口独立、空白/纯色裁片后筛。
已确认的 `curlie-00525691/mobile` 域名停放截图按PNG哈希隔离，其未复核同页视口待审。

## 全流程运行

```bash
python3 scripts/pipeline/cli.py run \
  --input datasets/curlie-full-10k \
  --workdir outputs/postprocess-components-v4 \
  --config scripts/offline_postprocess/config.yaml \
  --baseline outputs/postprocess-components-v3 \
  --workers 12 --pixel-workers 4 --pack-workers 8 \
  --prepare-output outputs/pack-input-components-v4 \
  --pack-output outputs/chunks-components-v4 --stop-after pack
```

同一pipeline已跑完预筛、后处理、裁片后筛、统计、独立校验、精简导出、压缩。
输入仍为1319页、3957个原始视口，不使用废弃v13标签。

| 指标 | v3 | v4 |
|---|---:|---:|
| accepted视口 | 3856 | 3604 |
| rejected视口 | 65 | 315 |
| review视口 | 36 | 38 |
| 可用裁片 | 11809 | 11348 |
| 可用切线记录 | 3653 | 3410 |

新增252个视口退出发布：249个横向溢出、1个已确认域名停放、2个停放页未复核视口待审。
连带撤出461张裁片、243条切线记录。历史250个横向溢出视口全部不再发布，
其中1个本来已被其他质量规则剔除。正常同页视口仍保留。

## 成品与核验

- 精简导出：`outputs/pack-input-components-v4`，1293个页面、3604张完整PNG及处理JSON，URL无缺失。
- 压缩成品：`outputs/chunks-components-v4`，8个tar.gz，压缩共4690978923字节；
  metadata按1293个页面分组，包内7208个PNG/JSON文件。
- 全量独立校验3957对源文件、11348张可训练裁片、3410条切线通过。
- 完整压缩分片及每个包内PNG/JSON的SHA-256核验结果写入压缩目录 `verification.json`。
- `reports/restoration-verification.json`记录旧删漏放集合与新清单比对、逐例状态及v3成品未改写证据。
- 原10例保持名单、编号：7个问题视口全部rejected且零裁片/切线发布，3个正常/低价值视口仍accepted。
  新图库：`reports/restored-filter-review10/index.html`。
- 25个离线/阶段测试、10个历史page_filter测试、12个历史pipeline测试、5个压缩测试通过，共52项。

横向layout仍是筛选代理，不证明CSS响应式；未引入通用OCR或停放页语义分类器。
v3目录和压缩包完整保留，当前交付使用v4。
