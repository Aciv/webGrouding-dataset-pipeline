# 独立层级后处理 v13

当前流程：**完整树规范化 → 各深度生成融合候选 → 模型判断 → 自底向上替换/拆解上层 → 严格补缺到 L4 → 其余细节归档 L5**。

完整规则与阈值：[第四层累计与第五层归档](ACCUMULATE_L4_ARCHIVE_L5.md)。v9 严格删除不再是默认行为。v11 修正 v10 将容器内部细节展平到第四层的问题：同父既有 L4 覆盖至少 95% 的候选留在 L5，新增候选之间不保留相互包含的框。

最终交付格式为 **真实 L1–L5 的 nodes＋图片**，详见 [最终导出](FINAL_EXPORT.md)。四层主体与归档仅是诊断表示，不再当作最终训练格式。

## 运行

```bash
cd responsive-ui-dataset
# 使用已有 config.local.json；首次才复制示例并填写接口、模型和 key。
python3 -m pip install -r scripts/layer_postprocess/requirements-pixels.txt

# 无 API 预览。必须从原始输入处理，不要接已严格删除的派生输出。
python3 scripts/layer_postprocess/cli.py \
  --input ../datasets/pages --output runs/layers-v12-preview \
  --config scripts/layer_postprocess/config.local.json \
  --dry-run --limit 100 --concurrency 4

# 实际模型判断：每份最多一次请求；没有候选则跳过 API。
python3 scripts/layer_postprocess/cli.py \
  --input ../datasets/pages --output runs/layers-v12-model \
  --config scripts/layer_postprocess/config.local.json \
  --limit 10 --concurrency 3

python3 scripts/layer_postprocess/compare.py \
  --output runs/layers-v12-preview --baseline runs/layers-vlm-09-strict-preview-42
```

输入/输出必须独立。API 为兼容 OpenAI 的 Chat Completions、图像 data URL 和 JSON 输出。
密钥放在已忽略提交的 config.local.json 或环境变量，不打印到日志。
像素路径使用 Pillow/NumPy；设 pixels.pixel_fallback_enabled=false 可关闭。
支持 --select、--resume、--reuse-edits。缓存须同版本、同原始输入、同规则准备与候选；不能把旧分组投票当成新替换批准。

## 输出

融合先于归档，详见 [流程与验证约束](FUSION_BEFORE_ARCHIVE.md)。dry-run 的四层预览只供查看，不作为融合输入。


- fused-hierarchy.json：模型处理后、归档前的完整树，用于最终五层导出。
- fusion-input-hierarchy.json：归档前的完整规范化树，模型实际使用此树，允许 L5+ 参与候选。
- rule-hierarchy.json：模型前的四层主体和第五层细节档案。
- candidate-hierarchy.json：安全执行后的候选；hierarchy.json 与原图副本用于正式处理输出。
- detail_archive.nodes：独立第五层档案，保留原 bbox、原层级、原 parent、对应主框 owner 和原因；默认训练只读取主树 nodes。
- layer-review.json：累计统计、尺寸组/惩罚、密度触发器、模型决定、实际调用 usage 和拒绝项。
- layer-postprocess-report.json：批次状态。
- comparison.html：自由选层，L1–4 看主体，L5 看归档。

第五层是非训练档案，不强行建立虚假的训练 parent 链。主树 parent 与层级保持一致，允许分支提前结束。
接受/结构通过不代表人工质量通过；模型可不修改，不把分组数量当成收益。

## 模块

- layer_accumulation.py：补入真正缺少的主框、对应/近同框去重、比例与相近尺寸频次、归档。
- replacement_groups.py：复用已有下层区域或拆解上层；原成员不加深。
- grouping.py / grouping_vlm.py：密度条件下的必要替换候选及一次模型判断。
- pixel_regions.py：子块分解失败后的 DCGen 式分隔候选；分区框与训练候选框不同。
- normalize.py / semantic_cleanup.py：复用既有几何清理和包装收缩；当前流程不启用严格语义删除。
- storage.py、compare.py：复用的存储与查看；analyze_deep_layers.py：批次分层诊断。
- legacy_pipeline.py、deep_noise.py：历史方案与回归参考，不是当前默认删除策略。

```bash
python3 -m unittest discover -s scripts/layer_postprocess -p 'test_*.py'
```

历史设计：SEMANTIC_AND_PIXEL_RULES.md、STRICT_REPLACEMENT_RULES.md、LEGACY_V4.md。
不改采集器、截图/DOM 对应或原始数据。
