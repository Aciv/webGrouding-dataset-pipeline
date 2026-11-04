# 四级视觉粒度归一 v3

## 目标与处理顺序

不以“深度已经小于四”为粒度正确的证据。每一级分别表示大区域、区内组件、主要内容、必要深层细节。
已有数据只有截图与 hierarchy.json；DOM 不可恢复，不重访网站。

1. 执行原有近同框压缩、兄弟重复吸收，保留来源和后代。
2. 检查大范围 root：已有多个 section/article/region 子块时，省略包装，直接提升子块。
3. 如果大 root 的孩子混有图片、文本、表格和导航，则从子框间的空白投影带提出分组；先组合，再提升。
4. 重连 parent，计算真实深度；最后才收缩深路径冗余包装或省略四级以外的细节。
5. VLM 结合原图审核分组，必要时重新分组、重挂或恢复源节点。
6. 重新执行规则归一，最终视觉审核通过后输出独立训练候选。

这是粗粒度标注政策，不保证所有层在所有页面上具有同样的像素大小；图片、表格等也不按面积强制切碎。

## 分组候选如何产生

`layout.py` 仅对宽泛的大区域包装工作，跳过原子图片/表格、语义 header/footer/aside/navigation，以及已经生成的新父组。
有可靠 section 子结构时直接复用，无需额外视觉切分。否则按孩子 bbox 的空隙递归分组，默认两次分割，可形成“左右分区＋区内上下分区”。
候选分界不能切穿任何参与分组的子框，也不把整张图均切为固定网格。没有足够证据时保持原树，交给 VLM 处理。
新父框使用批准成员的外接框。不会从整图裁剪框直接制造训练 bbox；子框已有粗糙边界时，外接框仍可能粗糙，需要审核。

借鉴 DCGen 的递归分解思想，**当前不是 DCGen 像素分隔器的复现**：不检测图片内部的颜色变化或空白像素，不新增图像依赖。
像素边界提名可以作为后续补充，不能把本次 bbox 空隙分组描述成已实现图像分割。
参考：[官方实现](https://github.com/WebPAI/DCGen/blob/main/utils.py)。

## 验证与对比

```bash
python3 -m unittest discover -s scripts/layer_postprocess -p 'test_*.py' -v

python3 -u scripts/layer_postprocess/cli.py \
  --config scripts/layer_postprocess/config.local.json \
  --input ../datasets/pages \
  --output runs/layers-vlm-03-layout-10 \
  --limit 10 --concurrency 2

python3 scripts/layer_postprocess/compare.py \
  --output runs/layers-vlm-03-layout-10 \
  --baseline runs/layers-vlm-02
```

对比入口为输出目录的 `comparison.html`。截图是原图硬链接/复制，框在浏览器里绘制，可固定在顶部自由多选层级，绿色标框，可显示 ID。
左侧可选择原标注或旧后处理（有对应文件时），右侧显示新版结果；未通过时明确显示候选，不冒充通过。
`comparison-summary.json` 记录逐样本框数量、各层数量、最大根框高度比例、VLM 问题和规则分组记录。

接口兼容问题和规则重复执行的问题修复后，可用相同指令加 `--resume`：跳过已通过且存在正式 hierarchy.json 的页面。
失败页如果已有有效付费编辑，会重建该编辑请求当时的规则候选再重放编辑，仅做后续验证；旧报告备份到 layer-review.previous.json。
未通过视觉审核的页面会重新请求编辑，不会伪造 accepted。
接口模型输出接受标准 JSON 对象、编辑操作数组或包住对象的单元素数组；不会把空验证数组当作通过。

## 已知局限

- 分组仍基于现有 bbox，缺失图文/段落目标不能仅凭空隙恢复；VLM 看到的边界也可能受长图缩放影响。
- 孤立的小标题、版权文字、角标可能因粗糙 bbox 被并到不合适的父组，需局部复核。
- 最终 accepted 是模型判断，并非人工真值。比较报告中的计数变化也不等于准确率提升。
- 原有 review disposition 及训练 ignore 掩码适配问题仍需下游单独处理。
- 不改变采集器、截图或 DOM 提取机制；本次只修改独立后处理目录。
