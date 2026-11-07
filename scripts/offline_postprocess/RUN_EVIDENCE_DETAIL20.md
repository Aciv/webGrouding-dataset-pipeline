# 20%覆盖率：仅细节裁片训练选择

用户明确要求：低覆盖率仅抛弃当前裁片的细节训练资格，不剔除整页、其他裁片或整页切线监督。

- 基础生成版本仍为component_slices_v4；选择策略为detail_crop_union_coverage_v1，阈值0.2，严格低于才抛弃。
- 原11348张裁片保留审计标记，其中421张detail_training_eligible=false、detail_training_discarded=true；10927张用于细节训练。
- 受影响277个视口；其中100个视口没有剩余细节训练裁片，但仍保留原图、质量状态与原有整页任务资格。
- 1293页面、3604通过视口、3410条有效切线记录不变。切线文件路径与SHA-256完全一致。
- 新成品outputs/chunks-components-v4-detail20：8个tar.gz、7208个PNG/JSON成员；每个成员/分片哈希及全部裁片标记核验通过。
- 页面JSON中crops保留被抛弃裁片及原因；detail_training_crop_ids与discarded_detail_crop_ids明确列出消费资格。细节训练只消费eligible=true。
- pipeline新增detail-filter阶段及stop-after，默认min-detail-coverage=0.2；prepare-pack和pack沿用选择索引，原生成缓存不重新计算。
- 本轮27个离线/选择/阶段测试、12个历史pipeline测试、5个打包测试通过（44项）；单独覆盖重叠面积不重复、20%边界保留、同页兄弟保留、切线不变、0细节裁片视口仍导出打包。
- 原始PNG/JSON、v4生成结果及旧压缩包未改写；未执行模型训练。

实际运行使用pipeline run --resume，detail-output为outputs/detail-selection-components-v4-20，prepare-output为outputs/pack-input-components-v4-detail20，pack-output为outputs/chunks-components-v4-detail20。
