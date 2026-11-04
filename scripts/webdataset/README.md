# WebDataset 分片打包与 Hugging Face 预览

独立于采集的后处理：读取已经保存的图片与 hierarchy，打成标准 WebDataset TAR 分片。仅 Python 标准库，不调用浏览器、不上传数据、不重新编码 PNG，不改采集源文件。复用根目录 export_pages.py 的 find_pages / training_files。

## 打包

在 responsive-ui-dataset 目录下：

```bash
python3 scripts/webdataset/cli.py \
  --input runs/curlie-full-10k \
  --output exports/curlie-full-10k-wds \
  --shard-size-mb 512 \
  --samples-per-shard 1000 \
  --require-url
```

传入采集根目录以便读取 collection-index.json / batch-index.json，不要只复制 pages 后丢掉索引。输入也支持 pages 或嵌套批次包装目录；索引缺少 URL 时依次尝试 hierarchy.capture.url、同目录 nodes.json 的实际 URL。都没有时标记缺失，绝不根据目录名推断 URL。

| 参数 | 默认 | 作用 |
|---|---|---|
| --input | 必填 | 含发布页面及来源索引的采集目录 |
| --output | 必填 | 独立的空输出目录；与输入不能互相包含 |
| --shard-size-mb | 512 | 每片目标 MiB；单个超大样本独占一片，可超过此值 |
| --samples-per-shard | 1000 | 每片最多截图样本数，两个限制先到先切片 |
| --seed | 42 | 固定种子打乱样本顺序，不产生数据划分 |
| --require-url | 关闭 | 任意样本缺少来源页面 URL 时拒绝打包 |

一张图及其对应 JSON 算一个样本。同一页三个分辨率算三个样本。不要在文件仍会变动时打包；Curlie export-only 以根索引已发布页面为准，隐藏临时目录不参与。

## 结果

```text
exports/curlie-full-10k-wds/
├── README.md                   # 带 data_files 声明的 HF 数据卡
├── manifest.json               # 完成标记、分片大小、样本数及缺失 URL 统计
├── samples.jsonl               # 样本→tar/成员、URL、分类、分辨率索引，无重复 hierarchy
└── data/
    ├── train-00000.tar
    ├── train-00001.tar
    └── ...
```

每片内部同前缀的文件相邻：

```text
sample-000000000.png
sample-000000000.json
sample-000000001.png
sample-000000001.json
```

JSON 包含：sample_id、page_id、source_id、**url（当前截图页面）、entry_url（配置入口）**、title、category、category_path、viewport、width、height、captured_at、annotation_rule_version、image_file、original_image_name、original_path、hierarchy。url_source 标明 collection-index.json、batch-index.json、hierarchy.json、nodes.json 或 missing。

`hierarchy` 完整保留原标签对象。它内部 capture.image 仍为原名 full.png/initial.png；包内 PNG 位置以外层 image_file 为准。需要原格式的训练代码应读 `metadata['hierarchy']`，不是把整个外层元数据当作标签。这里记录网页 URL，不额外收集每个 img 元素的网络资源 URL。

无类别写 null，统计中为 Unknown；不把内容主题强制映射为旧的六种功能类别。原始标注仍是规则候选，不因打包而成为人工核验 GT。

TAR 不做 gzip 二次压缩：PNG 已压缩，主要收益是减少小文件请求、顺序读取和分片传输。图片逐文件流式复制，不把全部 PNG 加载到内存。包内不包含 nodes.json、dom.html、叠框图或其他诊断文件。

完成后才生成 manifest.json 和 README；异常会保留 .partial，不把未完成分片暴露成正式 tar。不支持在非空目录覆盖或续写，请选新输出目录。源文件始终保留。

## 上传到 Hugging Face

```bash
pip install -U huggingface_hub
hf auth login
hf repos create 你的用户名/responsive-ui-data --repo-type dataset --private --exist-ok
hf upload 你的用户名/responsive-ui-data exports/curlie-full-10k-wds . --repo-type dataset
```

数据卡只声明 data/train-*.tar，避免 samples.jsonl 等辅助文件被当作另一份数据集。HF 的预览仍依赖服务端处理及账户权限；脚本不自动开通 Viewer，也不提供 layer 叠框预览。目录授权不代替网页内容授权，未自动给截图声明统一许可证。

## 下载及训练读取

```bash
hf download 你的用户名/responsive-ui-data --repo-type dataset --local-dir /data/responsive-ui-wds
```

或者先以 HF Datasets 流式检查：

```python
from datasets import load_dataset

dataset = load_dataset(
    "你的用户名/responsive-ui-data", split="train", streaming=True
)
sample = next(iter(dataset))
image = sample["png"]
metadata = sample["json"]
print(metadata["url"], metadata["viewport"])
hierarchy = metadata["hierarchy"]
```

这里 train 只是打包 split 名称，没有替你划分测试集。同一网站所有页面和分辨率应在后续划分时放在一起，避免泄漏。

## 模块与验证

- cli.py：打包参数。
- provenance.py：复用现有索引定位实际 URL、入口及分类。
- writer.py：配对检查、PNG 尺寸核对、分片、数据卡和索引。
- test_packaging.py：本地标准库测试；`python3 scripts/webdataset/test_packaging.py`。

参考：[HF WebDataset 格式](https://huggingface.co/docs/hub/datasets-webdataset)。未执行远程上传，不声称已验证某个私有仓库的在线预览。

实际验证：WordPress 整页三分辨率样本打为 2 个 TAR、3 组 PNG/JSON，URL 全部保留；在隔离环境中使用 Hugging Face Datasets 5.1.0，从生成目录自动识别 WebDataset，流式与非流式加载均通过，图片尺寸与 hierarchy/URL 对齐。样例位于 exports/wordpress-popup-webdataset，validation.json 保留结果。
