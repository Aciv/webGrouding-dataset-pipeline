# 历史 v4 全树修改与自审（非当前默认流程）

当前方案：[自底向上候选与第一级 60% 检查](FRONTIER_NORMALIZATION.md)。面积硬检查仅作用于最终第一级，不强制细分第二至四级。

请求现已默认使用[原图完整原始框＋精简当前结构＋变化信息](COMPACT_CONTEXT.md)，详细来源保留在本地；`--context-mode full` 可切回旧请求用于对照。

新增：[大区域拆解、子层重组与提升说明](LAYOUT_NORMALIZATION.md)。先解决粗粒度包装，再处理四级深度预算。

仅依赖 Python 3 标准库，读取已有 `full.png`（或 `initial.png`）和 `hierarchy.json`。
不调用浏览器、不重新采集 DOM、不改采集器。输入目录与输出目录必须互不包含，输出必须为空。

## 四级契约

| 人读级别 | JSON layer | 意义 |
|---|---|---|
| 第一级 | 0 | 导航栏、侧栏、主体内容分区、页脚等大区域；整页包装不占检测级 |
| 第二级 | 1 | 区域内的卡片、图文组、表格、表单、菜单组等组件/局部分组 |
| 第三级 | 2 | 组件内容：图片、文本、控件、表格行/单元格等；二三级为主要监督 |
| 第四级 | 3 | 仅在需要时保留的深层细节，可去掉低价值微碎片 |

这不是按类别强制分层：独立图片可以较早结束，复杂表格可继续分解。层号必须由 parent 推导；不允许只改数字。
初版不为第二/三级增加训练权重，该部分由训练管线实现。

## 配置与运行

```bash
cd responsive-ui-dataset
cp scripts/layer_postprocess/config.example.json scripts/layer_postprocess/config.local.json
# 编辑 config.local.json，填 base_url（含 /v1）、支持图像的 model。
export OPENAI_API_KEY='你的key'

# 免费预览：规则先生成四级候选树和报告，不调用 API，不生成确认后的训练标注。
python3 scripts/layer_postprocess/cli.py \
  --config scripts/layer_postprocess/config.local.json \
  --input runs/你的采集目录 --output runs/layer-proposals-01 --dry-run --limit 10

# 正式处理：每个样本两次 API 请求，先修改，再验证。
python3 scripts/layer_postprocess/cli.py \
  --config scripts/layer_postprocess/config.local.json \
  --input runs/你的采集目录 --output runs/layers-vlm-01 --limit 10
```

支持单个 hierarchy.json 或递归目录输入。`--limit 0` 表示全部，默认全部。目录中多个版本会分别处理，建议明确选择一个版本的 pages 目录。
配置 `concurrency` 为样本并发数，默认 1。`api_key` 也可直接填写在本地配置；示例保持空白，`config.local.json` 已忽略提交。
`request_options` 用于模型支持的附加参数，例如 `max_completion_tokens`、`reasoning_effort`；按你的服务实际支持情况填写。
接口使用 OpenAI 兼容 `POST {base_url}/chat/completions`、图像 data URL、JSON object 输出，服务须同时支持这三项。
模型推理可能远慢于本地树处理。若兼容服务支持，可在 `request_options` 显式设置其关闭深度思考的参数，例如某些 Qwen 服务的 `"enable_thinking": false`；不同服务并不保证支持同一参数，所以程序不会擅自添加。
参考：[OpenAI 图像输入文档](https://developers.openai.com/api/docs/guides/images-vision?api-mode=chat)。

## 实现与边界

- `normalize.py`：在请求 VLM 前实际执行规则，生成最多四级的粗粒度候选；VLM 调整后再次执行，深度超限不再直接报错。
- `layout.py`：提升合理 section 子层；对混合内容框依据子框间空隙先分组再提升，避免全部原子节点直接升到第一层。
- `frontier.py`：在粗糙包装下寻找混合深度的有效节点，标题与内容自底向上分组；识别排版 table 候选、严重裁剪残片，并校验第一级大框的明确豁免。
- `model_edits.py`：校验模型指令、记录拒绝项、规范新组 ID，并保护已经提升的导航/正文/页脚行不被重新打散。
- `compare.py`：生成原始/旧后处理/新结果的交互式框图对比和逐样本统计。
- `rules.py`：树检查、近同尺寸/兄弟重叠/孤立层/小碎片候选；执行 contract、drop、merge、group、reparent、restore。
- `vlm.py`：四级提示词、原图与简化树输入、超时及 token usage；API 错误不记录可能回显密钥的服务响应体。
- `cli.py`：文件扫描、并发调度、两阶段 API、每页隔离和输出。

规则先压缩近同尺寸父子、去除通用整页包装、吸收高重叠兄弟；路径仍过深时，优先收缩通用单孩子包装，再把第四级以下的必要省略细节逐叶记录为 ignore_regions。
这得到的是粗粒度候选，不证明四级语义已经正确；VLM 负责检查大区域、组件、内容与细节的分解，并可拒绝、新增分组、重挂或 restore 被规则省略的重要原节点。
截图＋候选树＋被省略节点的记录一起输入 VLM。原树缺失的框仍不能凭空恢复。
微碎片/孤立层候选必须视觉审核，不按每层数量强行平均分配。
contract 删除包装，后代提升；merge 吸收兄弟，两边后代保留；group 为兄弟建立外接框父组；drop 仅允许叶节点，并记录 ignore_regions。
新分组只使用成员外接框，类别固定为 block，不让 VLM 猜新 bbox 或新类别。
修改逐项检查无环、有效父节点、坐标与图像边界；层级由程序从 parent 重算。深层省略可追溯，绝不是只把 layer 数字改小。
最终 VLM 必须明确确认四级语义通过；不再要求模型抄写全部节点和层级，减少无意义的输出。若服务仍返回逐节点 levels，会继续严格检查一致性。
树请求改成列式紧凑 JSON，保留节点 ID、parent、bbox、类别等，不重复大量字段名。
原有 review disposition 不会擅自改成 detection：训练转换若整支跳过 review，仍需要单独处理，后处理不能宣称解决了该过滤问题。

成功目录含原图复制、hierarchy.json、layer-review.json；总目录含 layer-postprocess-report.json。
失败/未通过目录保留报告和已生成的 candidate-hierarchy.json（如有），不生成正式 hierarchy.json。免费预览也保存 candidate-hierarchy.json，便于查看具体规则结果。
新 parent_basis 和 annotation_status 是独立协议扩展，严格读取旧枚举的下游需要适配。
hierarchy.json 保留原始节点字段、源版本、操作和来源；新增父组不具有原 DOM selector。
**训练端必须使用 ignore_regions 掩码**；当前仅记录，不修改训练管线。VLM 审核不等于人工真值。
不会复制旧 collection-index，以免其成功状态/产物路径错误地描述派生数据。

当前每次发送原图及完整树，不做长图分块；长图细节可能被模型内部缩小，max_nodes/max_image_mb 超限直接报告失败，绝不截断输入。
此版本用于先验证规则和四级定义，尚不适合未经抽查就批量发布；每次使用新输出目录。
已有输出可显式加 `--resume`，跳过同版本已通过页面并重试失败；旧版结果会备份后从原始输入重新处理。`--concurrency` 可覆盖配置的并发数。

## 复用已经付费返回的编辑

同版本失败输出通常仍保存了 edit_response；可以复用有效编辑。v4 的分组顺序与旧版不同，不接受旧版编辑缓存，以免先丢掉重组需要的深层节点。

```bash
python3 scripts/layer_postprocess/cli.py \
  --config scripts/layer_postprocess/config.local.json \
  --input ../datasets/pages \
  --output runs/layers-v4-retry \
  --reuse-edits runs/layers-v4-failed --limit 2
```

按相同目录布局读取旧 layer-review.json，严格核对 source 路径，先重放旧编辑，再规则归一到四级，最后仅调用一次 VLM 验证。
旧报告缺失或编辑无效会在该页明确报错，不自动偷偷重新计费；可以先加 `--dry-run` 查看结果。
该选项用于同一份原数据，不能拿旧编辑处理已经人工修改过的源标注。
日志增加 vlm_started/vlm_finished、各阶段时间、服务返回的 token usage。API 错误也记录该阶段已用时间。

## 测试

```bash
python3 -m unittest discover -s scripts/layer_postprocess -p 'test_*.py' -v
```

测试覆盖后代保留、来源不变、忽略区域、循环/跨父合并拒绝、分组几何、四级上限、模拟图像 API 双阶段、失败样本隔离。
模拟 API 只验证工程协议，不证明真实 VLM 的清理准确率。
