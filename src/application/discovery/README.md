# 主页发现与完整多分辨率采集

同一 URL 使用一个 page：先采参考尺寸做去重，再在原页面依次切换其余尺寸。这里的“一次采完”指连续完成整组，不是同时改变一个 page 的多个尺寸。不会为每个分辨率导航或重放。

| 文件 | 职责 |
| --- | --- |
| collect-sites.ts | 共用 Chromium、来源并发、退出清理 |
| state.ts | 恢复、必需文件核验、串行检查点 |
| site.ts | 主页候选清单、访问预算、来源状态 |
| visit.ts | 单 URL 的导航、去重、采集、失败隔离 |
| publication.ts | 分配 ID、暂存参考图、检查 URL、整组提交 |
| references.ts | 从已有首屏或 full.png 顶部恢复图片比较参考 |
| types.ts | 流程上下文 |

浏览器逻辑在 ../../browser/discovery/；URL/图片比较规则在 ../../domain/discovery/；逐尺寸采集共用 ../capture-page.ts。DOM、bbox、层级和结构标签仍由原 browser/capture.ts 流程生成。

## 提交与恢复

1. 只从主页链接和主页展开菜单收集同源候选；子页不继续扩展。
2. 参考图放入 .discovery-staging，检查 URL、图片重复。
3. 在同一 page 连续采完配置中全部尺寸，并复查保存的 URL。
4. 整组成功才复制到 pages/<id>，加入 batch-index.json 与 acceptedPageIds。
5. 失败记录保留在 discovery-index.json 的 visits 中，包括 candidatePageId、viewportResults、error；失败候选不占名额。

来源 partial 表示完整页数量不足，不表示允许正式数据缺尺寸。中断会保存已提交整组；尚未完成的组在 resume 时重采。正式索引不引用临时文件。检查点写入采用不可变快照与写入队列，两个索引间中断以页面主索引为准恢复。修复旧数据时保留人工核验文件。
