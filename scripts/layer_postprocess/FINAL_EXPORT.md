# 最终训练输出：清洗后的 L1–L5

最终输出目录只包含图片和 hierarchy.json，不包含融合候选、归档、审核记录、预览或其他中间文件。

```text
export-root/
  pages/<page-id>/<viewport>/
    full.png             # 或 initial.png，以 capture.image 为准
    hierarchy.json
```

## 标签含义

- 输入是经过规范化、批准融合/上层替换、冗余包装收缩后的 fused-hierarchy.json。不是原始 DOM，也不是归档数组。
- 按实际 parent 链保留前五级，L1 对应 layer=0，L5 对应 layer=4；L6+ 截断，不提升成第五层。分支可提前结束。
- 最终输出是五层训练标签；用于后处理预览的四层主体＋第五层档案是另一种诊断表示，不能拼接后冒充五层训练树。
- schema、capture、nodes、decisions、ignore_regions、stats 等沿用原 hierarchy.json 格式。bbox 保持清洗后数值，parent 保持清洗后关系，layer 与 stats 重新计算。
- 原始节点字段用明确白名单保留；accumulation_source、postprocess、detail_archive、region_replacements、尺寸组、模型响应等不进入最终标签。decisions 为空，不复制冗长修改历史；保留原格式 ignore_regions，避免误把已明确忽略的前景当作背景。
- 已有的 capture.url 保留；annotation_status 仍为自动规则候选，不宣称人工真值。
- 原始输入与处理中间数据不覆盖、不删除；“只保留两个文件”指最终导出目录。

## 处理并导出

```bash
cd responsive-ui-dataset
python3 scripts/layer_postprocess/cli.py \
  --input ../datasets/pages \
  --output runs/layers-v13-processing-100 \
  --config scripts/layer_postprocess/config.local.json \
  --limit 100 --concurrency 6 \
  --export-to runs/layers-v13-clean-100
```

已完成处理后可单独导出，无 API 调用：

```bash
python3 scripts/layer_postprocess/clean_export.py \
  --input runs/layers-v13-processing-100 \
  --output runs/layers-v13-clean-100
```

输出目录必须独立且为空；导出前检查所有观察均有结构通过的 fused-hierarchy.json 和尺寸一致的原图。失败或待审项会阻止导出，不暗中跳过。v12 及之前没有持久化最终完整融合树，不能用四层归档结果反推本导出。
