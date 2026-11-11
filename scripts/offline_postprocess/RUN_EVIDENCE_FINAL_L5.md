> 历史记录：本文件描述旧的 L6+ 无条件截断版本。当前 `layer-filter` 已改为 L6+ 大框吸附后按 L5 覆盖补入 owner，见 `scripts/pipeline/README.md`。

# 最终L1–L5成品：保留融合与分割机制

按用户最新确认，最终仅保留融合后实际parent深度0–4（L1–L5），不提升深层节点。
旧完整图和包保留审计，当前成品为outputs/chunks-components-v4-l5-detail20。

## 保留机制与全量证据

原小框融合、内容块聚合、包装清理、局部DCGen式分解、全局切线和上下文机制均保留。
3957个视口逐项对照：operations、partition、split_label、crop_bbox、core_bbox完全不变；
所有保留节点的ID、parent、bbox、struct/subtype、监督资格和来源映射保持不变。
证据见outputs/postprocess-components-v4-final-l5/reports/mechanism-preservation-verification.json。

## 输出变化

- 通过视口仍3604个，页面仍1293个，整页切线标签仍3410条。
- 质量通过图由508868节点降到400643，检测目标由256388降到215903；L6以后40485个检测目标退出成品。
- 保留10095个有效生成融合/分解组；不恢复已被代表的小碎框。
- 用L1–L5框重算20%覆盖率后，10849张细节裁片保留，499张仅作细节训练抛弃标记。
- 最终细节训练有211994个去重目标、222376次裁片框出现。当前层统计仅L1–L5；原始source_layer另表供审计。
- 新包8个tar.gz、7208个PNG/JSON成员，全部节点真实深度≤4、正样本及ignore引用完整，分片/成员SHA-256独立校验通过。
- 30个离线/阶段/截层测试、12个历史pipeline测试、5个打包测试通过；实际浏览器验证逐层表、随机抽样、搜索、放大和细节抛弃标记通过，无网络请求/JS错误。

## pipeline顺序

filter → postprocess → final-filter → layer-filter → detail-filter → prepare-pack → pack。
生成阶段仍保留完整中间图。layer-filter之后只发布L1–L5，detail-filter重算覆盖率；
单独prepare-pack也会补齐这两个阶段，packer独立检查，不能直接打包未截层component索引。

统一查看器outputs/dataset-viewer/index.html已重建并指向最终L5索引。
