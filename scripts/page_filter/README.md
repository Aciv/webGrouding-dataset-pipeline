# 当前两阶段filter

当前实现位于 `stages.py`，通过 `scripts/pipeline/cli.py dataset` 调用：

1. `pre_filter_source`：处理前校验原始xywh候选几何和layout代理，少框不自动失败，layout默认拒绝严重溢出；
   PNG解码/尺寸/来源指纹、已确认错误页和像素预筛由同一流水线协同执行。
2. `post_filter_crops`：在画框之前的RGB像素上检查每个实际裁片及核心区域；检测纯色和近空背景。
   不依赖DOM框数，不把灰度方差低直接当成纯色，保留等亮度但色彩不同的真实内容。
3. 任一裁片命中，整个视口及所有检测/切线监督退出主索引；其他视口独立判定。

阈值在 `scripts/offline_postprocess/config.yaml` 的 `pre_filter`、`blank_crop_filter` 中配置。
v4按历史 `9f62b6c` 恢复原阈值：横向超出2px且丢失宽度至少10%计为问题框；
至少5个问题框且占比≥20%时拒绝该视口。保持逐视口独立，不恢复旧版整页连带剔除和少于10框门槛。
已视觉确认的域名停放截图按SHA-256隔离，未复核的同页其他视口待审；不声称实现了通用语义识别。
失败区域、最大颜色跨度、前景/边缘比例写入审计和 `reports/blank-crops.jsonl`。
`responsive_status=unknown` 明确表示只有几何代理，不能据此证明网页CSS响应式正常。

这是用户对原计划“局部挽救”的显式收紧：有空白/纯色裁片时不再保留该视口的其他好裁片。

---

以下CLI和规则是历史几何筛选实现，不是当前两阶段流水线的完整入口。

# 已采页面离线筛选

只生成JSON清单，不删除、复制、裁剪或改写图片/标签；不启动浏览器，不需要Node、Pillow或模型。Python 3.9+，仅标准库。

需要与后处理和分块打包串成按索引交接的流程时，见 [索引式离线 Pipeline](../pipeline/README.md)；本脚本仍是独立的单次筛选入口。

## 运行

在responsive-ui-dataset目录：

```bash
python3 scripts/filter-valid-pages.py \
  --input runs/curlie-full-10k-clean runs/curlie-homepage-10k \
  --output runs/valid-pages.json
```

支持一个或多个采集根、包含versions目录的根、导出目录或pages本身。顶层Curlie collection-index.json存在时按已发布索引读取，忽略.work等临时采集产物；否则发现pages目录。不同规则版本、不同根下同名page不覆盖；重叠输入的相同文件去重。

默认必须具备desktop、mobile、tablet三组图片+hierarchy.json。任一尺寸无效，整页进入rejected_pages；所有尺寸通过才进入valid_pages。只检查一个尺寸可指定 `--viewports desktop`，但这不代表通过多分辨率筛选。

## 默认规则

1. **每张图分别统计nodes中全部layer的框**，数量 `<10` 剔除；恰好10个通过。不是把三尺寸加总。
2. 大量越界：某尺寸至少5个问题框，且占该图全部框的比例 `>=20%`，剔除整页。
3. 最终训练bbox按 `[x,y,width,height]` 与实际截图范围比较，允许2px边界误差。
4. 原layout_bbox通常保留未裁剪DOM几何。额外统计其横向越界：超过2px且丢失宽度达到原宽度的10%时算问题框。最终bbox和layout同时异常的框只计一次。
5. layout_bbox的底部超高不计响应式失败，避免将正常full图最大高度截断判错；最终训练bbox在任何方向明显越界仍计数。
6. 图片缺失、标注JSON损坏、无效/非有限/非正尺寸bbox、PNG尺寸与capture不一致，判该尺寸无效。单页坏数据不会终止其他页。

**横向layout溢出只是响应式问题启发式，不是确定的UI故障证明。** 合法轮播/局部裁剪也可能触发；可调整阈值或用 `--no-check-layout-overflow` 只检查最终训练bbox。缺少layout_bbox时保持unknown，仅检查已有bbox，不凭空断言正常布局。

阈值可调整：

```bash
python3 scripts/filter-valid-pages.py \
  --input runs/curlie-homepage-10k \
  --output runs/valid-pages-strict.json \
  --min-boxes 10 \
  --min-outside-boxes 5 \
  --max-outside-ratio 0.2 \
  --tolerance-px 2 \
  --min-layout-overflow-ratio 0.1 \
  --workers 8
```

本脚本不根据文本语义识别403/验证码页面；若它们拥有足够框也可能通过。原标签已删掉的完全离屏元素无法恢复，layout统计也只针对当前保留节点。

## JSON结果

- `summary`：有效/无效页数、有效图片数、原因分布和可获得的分类统计。
- `valid_pages`：可用页面清单，每页含id、原URL/分类（原索引有才保留）、input_root、page_directory和各分辨率的图片/标注路径与统计。
- `rejected_pages`：无效页及具体尺寸、原因、越界数量/比例，最多5个问题节点示例。
- `rules`：使用的阈值和版本。

每页的 `viewports.<viewport>.image/hierarchy` 都相对于该页 `input_root`。这是一份独立筛选清单，不是改写过的collection-index；不会改变原采集数量，也不会自动让训练/打包程序采用过滤结果。

查看统计：

```bash
python3 - <<'PY'
import json
with open('runs/valid-pages.json') as f:
    result = json.load(f)
print(json.dumps(result['summary'], ensure_ascii=False, indent=2))
PY
```

## 验证

```bash
python3 -m unittest discover -s scripts/page_filter -p 'test_*.py' -v
```

9项确定性测试覆盖10框边界、越界数量与比例双阈值、误差容忍、已裁剪bbox的layout横向异常、正常长图截断、非法几何、三尺寸整页判定、图片尺寸、索引来源、输入去重和原文件不变。

2026-11-05本地真实样本检查：`runs/quality-filter-desktop-check.json`。当前datasets/pages仅为95页的分尺寸抽样，不是完整三尺寸数据；其中33页具备desktop图片标签，32页通过、1页因框数不足被剔除。其他62页缺少desktop训练对，不代表原始采集网站失败。该次检查不能用于估计服务器10k数据的质量或响应式故障率。
