# 独立四级视觉层级后处理 v5

当前默认：**规则规范化 → 候选区域组合 → VLM 判断候选 → 程序分组与检查**。
不改采集器、截图、原始 DOM 或原始 hierarchy.json。仅写独立输出目录。

## VLM 只负责组合判断

- 原图与所有原始框仍完整提供；当前有效树单独给出，不让旧框充当当前节点。
- 规则在各保留深度查找同父、邻接、空间空隙及标题内容组合，候选全页最多 64 个。
- 模型对每个候选返回 accept / reject / uncertain；accept 可以选择至少两个候选成员。
- 禁止模型删除、恢复、任意重挂、修改 bbox、生成新候选或给全树通过结论。
- group 创建共同父节点，bbox 使用成员外接框，成员和后代都保留。
- 同一节点不能同时进入两个获批组合；不得超出四层预算。非法建议记录，不应用。
- 不再进行第二次模型自审。有候选每页一次调用，无候选零调用。
- 分组后不再执行可能删除成员的第二轮 normalize，避免组合收益被悄悄抵消。

已有确定性 normalize 中的压缩、清理、区域提升与分组保持原规则；它们并未全部交给
VLM 否决。该版本验证的是新增候选组合，不是全树视觉正确性。
候选只使用规范化后仍存在的节点，不能恢复此前已丢弃的原始深层细节。

## 运行

```bash
cd responsive-ui-dataset
# 使用已有私有配置；首次使用再复制 config.example.json 并填写接口、模型和 key。
python3 scripts/layer_postprocess/cli.py \
  --input ../datasets/pages \
  --output runs/layers-grouping-01 \
  --config scripts/layer_postprocess/config.local.json \
  --limit 10 --concurrency 3

# 仅规则及候选预览，不付费
python3 scripts/layer_postprocess/cli.py \
  --input ../datasets/pages \
  --output runs/layers-grouping-preview \
  --config scripts/layer_postprocess/config.local.json \
  --limit 10 --dry-run

python3 scripts/layer_postprocess/compare.py --output runs/layers-grouping-01
```

支持 --select、--resume、--reuse-edits。同版本缓存的规则准备和候选必须完全一致；
旧版全树 operations 不可复用为候选投票。--context-mode 仅保留旧命令兼容，
当前分组模式始终使用单次完整原始证据请求。

配置保留 api、rules、concurrency。新增 grouping.max_candidates（默认 64，1..256）
与 grouping.group_max_gap_px（默认 48 CSS px）。不应靠扩大候选数代替视觉质量验证。

## 输出语义

- layer-review.json：rule_operations、group_candidates、group_response、applied_group_operations、
  rejected_operations、unresolved_candidates、实际调用时间和 usage。
- rule-hierarchy.json：模型调用前的纯规则基线，方便单独评价 VLM 增益。
- candidate-hierarchy.json：始终保留可用的派生候选。
- hierarchy.json 与原图副本：仅在已执行的组合有效、局部结构检查通过时发布。
- 未返回或 uncertain 的候选保持原树，记录 grouping_warnings，不当作批准或整页否决。
  非法组合或仍未解决的非原子 L1 大框（面积 >60%）标记 needs_review。
  不借用模型组合决定冒充大框豁免。原子大图仍按既有规则豁免。
- accepted 仅表示本流程的结构及候选处理条件通过，**不是人工质量评分**。
  annotation_status=rule_candidate_vlm_grouped_unverified；whole_tree_vlm_verified=false。

四级含义不变：L1 页面大区域；L2 组件分组；L3 主要内容；L4 必要细节。
训练仍须处理 ignore_regions。新父区域没有 DOM selector。

## 模块与验证

- grouping.py：纯规则候选与严格投票执行；复用 layout.partition、rules.apply_operations。
- grouping_vlm.py：单次候选提示词，继承 vlm.Client 的 HTTP、超时、凭据与 usage 处理。
- grouping_pipeline.py：规则 → 单次判断 → 局部检查 → 独立发布。
- storage.py：复用的原图尺寸校验和原子 JSON 写入。
- cli.py：文件扫描、并发和恢复；compare.py 继续复用分层对比网页。
- legacy_pipeline.py / LEGACY_V4.md：历史全树方案，仅用于旧实验和回归，不由当前 CLI 调用。

```bash
python3 -m unittest discover -s scripts/layer_postprocess -p 'test_*.py'
```

历史设计参考 [自底向上规则](FRONTIER_NORMALIZATION.md)、[空间划分](LAYOUT_NORMALIZATION.md)。
[精简上下文](COMPACT_CONTEXT.md) 和旧 token benchmark 描述的是历史双阶段实验。
