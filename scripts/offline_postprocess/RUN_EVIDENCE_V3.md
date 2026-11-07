# v3两阶段filter与pipeline真实运行

本轮落实用户对空白/纯色裁片的严格剔除要求，并将filter实际接入统一pipeline；原始文件只读。

命令：`python3 scripts/pipeline/cli.py dataset --input datasets/curlie-full-10k --workdir outputs/postprocess-components-v3 --config scripts/offline_postprocess/config.yaml --baseline outputs/postprocess-components-v2 --workers 12 --pixel-workers 4`。

输入3957个视口，状态为 `{'accepted': 3856, 'rejected': 65, 'review': 36}`。处理及清单发布650.83秒；统计、校验另计。

## filter与发布

- 第一阶段：`{'accepted': 3920, 'rejected': 26, 'review': 11}`；少框不自动判坏，layout溢出为警告，responsiveness仍为unknown。
- 裁片后筛检查12308个视图；58个实际裁片、82个core命中，去重后82个裁片视图。
- 39个视口因任一实际/core裁片命中而整体剔除，连带213个裁图视图，其中171个此前可训练。42个命中视图仍有标注目标，说明不能以DOM框数量代替像素质量检查。
- 最终发布11809个检测裁片和3653条整页切线监督。独立检查没有发布任何空白标记裁片或其同视口切线标签。
- index-filtered/index-postprocessed/index-final记录阶段资格、来源和派生指纹；最终索引经独立校验后发布，并引用通过筛选的清单。历史VLM/五层命令明确标为legacy。

## 05号样本回归

- curlie-00465577-p001的三个视口均独立命中空白裁片，全部退出发布。desktop命中0002、0003，mobile命中0002、0003，tablet命中0003、0004、0005。
- 第一阶段该样本几何通过，layout横向溢出记录为0，不能据此声称CSS正常；第二阶段按原始RGB判纯色，不能被幽灵DOM框掩盖。
- 正常对照curlie-00212383-p001三个视口均保留；单视口失败不自动删除其他正常视口。

## 原32例复核

- 原随机样本及编号完全不变，没有补抽替换。31个accepted、1个rejected，05号明确显示剔除原因。
- 共98个审计裁图视图，实际发布93个。灰色框为被剔除数据的审计候选，不是训练标签。
- 图库：`outputs/postprocess-components-v3/reports/random32-replay-20261107/index.html`；原始分辨率图片和ZIP均保留。

## 测试和范围

- 21个离线/新filter测试及12个历史pipeline回归测试通过。
- 独立全量校验3957对源文件、11809个训练裁片、3653条切线标签通过；三份索引引用和发布资格检查通过。
- 这不是CSS响应式自动验证器，也没有新增OCR或通用错误页识别模型。RGB阈值为显式可配置质量政策，不把代理指标称为人工真值。

详见运行目录reports/filter-summary.json、blank-crops.jsonl、publication-checks.json及validation.json。

## 精简导出、压缩与总体pipeline接续

- 最终导出 `outputs/pack-input-components-v3`：1296页面、3856视口，每视口仅PNG和精简JSON；
  URL无缺失，保留11809条裁片记录和3653条有效切线；排除65拒绝+36待审视口。
- 压缩成品 `outputs/chunks-components-v3`：8个tar.gz，压缩总字节4846659297；
  metadata.jsonl为1296页面索引，包内共7712个PNG/JSON文件。
  reports/pack-input-checks.json和压缩目录verification.json记录全量文件/分片SHA-256校验通过。
- 当前run默认组件引擎，五个stop-after阶段已通过本地固定样本的停止/继续/成品复用测试；
  来源变化选新目录、配置不匹配保护已发布索引也已覆盖。
- 真实全量 `run --resume --stop-after pack` 指定上述导出与压缩目录后：final-filter跳过，
  prepare-pack resumed=true，pack phase=skipped。未重新生成或改写现有压缩包。
- 最新回归：23个离线/阶段测试、12个历史pipeline测试、5个压缩测试通过。
