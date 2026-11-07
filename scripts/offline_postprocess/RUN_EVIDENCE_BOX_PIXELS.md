# 裁片内纯色框占比规则：实际数据证据

本次从现有 validated v4 最终L5图重新执行 detail-filter → prepare-pack → pack，
没有重新采集，也没有改变融合、分割坐标或原始截图。旧输出保留。

## 规则

最终正样本框逐个取原始RGB像素；每通道极差≤4视作纯色/近纯色。
按框数量计算，≥60%即标记当前裁片像素无效；重叠框单独计数，ignore不计。
空框集合不判断。仍保留20%框并集覆盖率规则，两条规则均通过才能用于细节训练。
这是像素启发式，不能证明加载/可见状态，也不能清除所有噪声。

## 全量结果

- 候选11348片；纯色规则命中62片，其中1片与低覆盖规则重叠，新增排除61片。
- 训练保留10788片，合计标记抛弃560片；涉及342个视口。
- 3604个通过视口、1293个页面、3410条切线监督均保持；105个视口无细节训练片。
- 细节训练211196个去重目标、221530次框出现；当前层统计仍仅L1–L5。
- 固定种子60抽查5张命中裁片，框大量落在空白区域，图中红框为纯色框。
  证据：`outputs/detail-selection-components-v4-l5-boxblank60/reports/pixel-rule-sample5.png`。

## 产物与验证

- 标记索引：`outputs/detail-selection-components-v4-l5-boxblank60/index-final.json`。
- 打包输入：`outputs/pack-input-components-v4-l5-boxblank60/pack-index.json`。
- 成品：`outputs/chunks-components-v4-l5-boxblank60`，8个tar.gz。
- 统一查看页：`outputs/dataset-viewer/index.html`，含纯色框计数、比例门槛和失败原因。
- 31个离线/截层/阶段测试与17个历史pipeline/打包测试通过。
- 实际Chromium离线打开、抽样、搜索、保留/抛弃片、放大、下载和逐层统计通过，
  没有HTTP请求或JS错误；详见查看页旁browser-verification.json。
- 成品7208个PNG/JSON成员及8个分片的SHA-256全部独立核对通过；
  包内每张裁片的60%边界、像素有效标记与训练资格全量一致，详见成品verification.json。
