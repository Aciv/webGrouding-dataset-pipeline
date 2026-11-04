# DOM tag → struct 独立规则模块

主入口是 `classifyStructure(node, context)`。模块只依赖原始 DOM 类型和本目录纯函数，不引用 Playwright、文件系统、核验网页或 hierarchy 代码。

```text
collect / layers / 核验保存验证
  → buildVisualHierarchy(snapshot)
    → projectVisibleNodes(snapshot)             共享可见性与面积过滤
    → createStructureContext(snapshot, visible) 每张观测创建一次
    → hierarchy/candidates.ts                  薄适配器
      → classifyStructure(node, context)       唯一分类入口
    → geometry / atomic ownership / parent     现有几何与归属处理
    → hierarchy.json                           分类、结构与原因一起输出
```

## 文件职责

| 文件 | 职责 |
|---|---|
| index.ts | 对外 API 与类型 |
| types.ts | 九类 struct、用途、分类结果与上下文接口 |
| context.ts | DOM/可见子节点索引、缓存祖先与后代计数 |
| evidence.ts | 原生/显式控件角色、绘制与伪元素证据；供几何模块复用 |
| decision.ts | 构造保留与排除结果 |
| classify.ts | 固定规则优先级、版本、非独立 tag 排除 |
| rules/controls.ts | 控件、媒体链接、整卡链接 |
| rules/content.ts | 文本、图形、图片、媒体、未解析嵌入 |
| rules/containers.ts | 区域、列表、表格、语义分组 |
| rules/fallback.ts | 通用 block、布局结构、待审 unknown 与无意义包装 |

## 九类与用途

`text / image / graphic / media / control / table / list / region / block`。

- `block` 取代草案中的 struct=group；`kind=group` 仍表示结构粒度，没有改名。
- `unknown` 仅为待审状态，不列入九个已知类别，也不兜底为 Text。
- `detection` 是按规则具备检测资格，仍是未经人工确认的候选。
- `structure_only` 保留组织关系，不等于已经导出为训练目标。
- `review` 保留有问题或证据不足的对象，例如未解析 iframe、混合正文等待分段。
- 几何/广告等排除继续写 decisions 和 ignore_regions。不能仅凭 disposition 字段声称训练损失已屏蔽。

## 优先级

1. 共享投影先处理截图、祖先裁剪、特殊隐藏和面积。
2. 排除元信息、换行等不独立 tag。
3. 原生和明确 role 控件；a 的单媒体与复合卡片在这里特殊处理。
4. 图片、图形、媒体与未解析嵌入。
5. 页面 region、表格、列表、语义分组。
6. 文本块及包含链接/媒体的混合内容。
7. 独立文字/背景图/伪元素、绘制 block、仅组织布局、unknown 或排除。

`class=button/card/nav` 不构成强类型证据；`div role=button` 是明确控件证据。disabled 控件不因状态而排除。可见空 href 的 a 仍是链接。

## block 与 unknown 的边界

- 已能确认是独立可见内容块，但没有更专门类别 → block。
- flex/grid 的间距只能说明布局组织 → block + structure_only。
- 只有交互提示却缺少原生/role 证据 → unknown + review；不能吞掉真实子控件。
- 纯透明包装、无独立内容与组织证据 → excluded，不强制分配 block。
- 原始节点始终保留；候选排除不删除 nodes.json。

## 版本与字段

分类策略：`dom_structure_v1`，写入 `structure_rule_version`。几何/层级组合版本独立写入 `rule_version`。

每个保留节点新增：`struct`、`subtype`、`disposition`、`semantic_role`、`text_role`、`source_node_ids`、`structure_reasons`。现有 node_id/tag/bbox/parent/layer 沿用。媒体链接来源可以包含 a/picture/img，多节点归属不会变成重复检测对象。

批次汇总提供 `by_struct`、`by_disposition`。核验网页在视觉模式可按 struct 和用途筛选，详情查看判断依据；原始模式不套分类筛选。

## 使用与测试

```bash
# 在 responsive-ui-dataset 根目录
npm test
npm run layers -- --dir runs/real-url-10-new-network
npm run review -- --dir runs/real-url-10-new-network --port 4312
```

正常 collect 会自动调用；layers 从已有 DOM 离线重算。不存在另外一套自动/手工分类规则。新采集增加 controls/disabled/open/multiple/size 等属性观测；旧快照缺失属性不伪造。

`src/tests/structure.ts` 覆盖九类映射、block/unknown/excluded、role/class 边界、媒体链接/picture、表格内部、原始事实保持与主流程；真实浏览器测试覆盖筛选和核验。

## 尚未实现的草案部分

- 混合段落尚未生成多个正文片段对象；标为 review，不认证为完整无重复文本监督。
- 没有跨 viewport 实体匹配、几何重建父节点、最深层整体删除。
- 没有 COCO/YOLO 导出与训练损失屏蔽；structure_only 仍保存在完整结构树。
- 不根据原生 option 存在就声称采集了浏览器原生弹出菜单。

标签定义参考项目草案及 Webshot 的结构/原子分工；未复制其 55 类、VLM、全局 NMS 或几何父关系重建。原 candidate 规则已迁移并按职责拆分，hierarchy 仅负责适配、几何和关系。
