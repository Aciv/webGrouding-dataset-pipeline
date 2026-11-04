# 精简 VLM 上下文与 token 对照

## 保留的信息

首轮先提供原图及**全部原始 bbox、类别、节点 ID**，不只给规则筛过的框。
传输坐标保留到 0.1 CSS px，训练 JSON 的原始精度不变。
完整原树的 parent/layer、原因、来源、操作历史仍在本地，不把过时 DOM 深度当成当前视觉层级目标。

模型首轮接收三条消息：

1. 系统：四级视觉契约、60% 检查和可用编辑。
2. 原始证据：原图、全部原始框及 ID。框上的 active 标记区分候选中仍保留的框与历史包装框。
3. 当前候选：完整的精简父子关系（roots/children），新分组或改动框的几何覆盖，规则操作短摘要。

当前候选的类别和 bbox 复用原始框；只有新增/变化几何才再次给出。当前有效拓扑是权威，不从原始 DOM 层级猜当前结果。

## 二次审核

保留首轮原始证据消息及模型编辑回复。过期候选结构被当前结果替换，避免同时保留两份完整树。
追加：

- 当前结果完整的 roots/children ID 关系表，避免模型将“变化表未列出”误读成“节点被删掉”。
- 受影响节点、祖先和少量邻近兄弟的前后 parent/layer。
- 新增或真正变化的 class/bbox；未变几何引用历史，不重复坐标。
- 子树整体移层摘要，不逐个展开所有仅层级变化的后代。
- 简短操作代码、被拒绝指令、尚未解决的一级大框。

详细解释不进入请求；仍保存在本地 layer-review.json 和 hierarchy.json 的来源记录中。
最终阶段明确要求 accepted verdict，不能用返回编辑指令冒充审核。

逻辑会话中图片只在原始证据消息出现一次；Chat Completions 每次请求仍重放所需历史。
**这不意味着图片只计费一次**，也不保证命中缓存。服务返回 usage 是实际依据。

## 使用

`delta` 已是默认模式，无需修改私有 key 配置：

```bash
python3 -u scripts/layer_postprocess/cli.py \
  --config scripts/layer_postprocess/config.local.json \
  --input ../datasets/pages --output runs/layers-compact-new \
  --limit 10 --context-mode delta --concurrency 3
```

需要旧请求格式对照可用 `--context-mode full`。规则、最大层数、图片质量与思考配置没有因此改变，仍为编辑、审核两次调用。

固定同一份最终标注，避免模型自由编辑内容不同导致比较失真：

```bash
python3 scripts/layer_postprocess/benchmark_tokens.py \
  --config scripts/layer_postprocess/config.local.json \
  --baseline runs/layers-vlm-04-frontier-3 \
  --output runs/token-benchmark-new --concurrency 3
```

此命令为每个样本新增一次真实审核请求，复用已记录的模型编辑，并与原始完整请求的 usage 对比。
不重写被审核的训练数据；新模型 verdict 可能不同，必须与 token 一起报告。

## 实验注意

完整自由编辑会改变最终树，故整体 token 变化同时包含“模型改了多少”和“推理长短”的随机变化。
固定目标对照可以隔离输入信息量；输出/推理 token 仍不是确定值。
早期仅给局部补丁的实验会导致模型误读旧树、将未列出的孩子误当丢失；因此当前保留完整精简拓扑，而非为省 token 删除必要信息。

本地测试覆盖原始 ID 完整性、当前结构可重建、变化补丁可重建、子树移层压缩、原图不重复添加和阶段格式切换。
