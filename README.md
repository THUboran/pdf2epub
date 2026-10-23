# PDF2EPUB 📚

把 PDF 转成**结构完整、可重排**的 EPUB 3：先由 marker 做版面还原得到 Markdown，
再按书籍目录切成按章文件，最后打包成带层级目录与页码锚点的 EPUB。

> 本仓库基于 [overcuriousity/pdf2epub](https://github.com/overcuriousity/pdf2epub)（MIT）改造。
> 原版把整本书输出成单个 Markdown，导致 EPUB 只有一章、目录只有一条。
> 本次改造的核心就是「把 marker 已经算好的章节结构真正接进来」。

## ✨ 相对原版的改造

| 类别 | 改动 |
|---|---|
| **章节结构** | 读 marker metadata 的 `pdf_toc` / `computed_toc`，按目录把整本 Markdown 切成 `NN.md`，EPUB 一章一个 XHTML |
| **目录** | navLabel 改用章节标题（不再是文件名）；按 `level` 生成**嵌套** `<ol>`；NCX 同步 |
| **页码** | 打开 marker `PAGINATE_OUTPUT`，把 `{页码}` 分隔块转成 `id="page-N"` 锚点，生成 EPUB 3 `page-list`，便于按纸质页码引用 |
| **合规** | 每个 XHTML 补 `<title>`（修 EPUBCheck `RSC-005`）；补 `landmarks`；`dc:language` 不再硬编码 `en` |
| **图片** | `.tif/.bmp/.webp` 等统一转 jpg，并按**实际写入路径**登记 manifest（原版会静默丢图 + 留下断链 `<img>`） |
| **标识** | `dc:identifier` 改用正文内容 SHA-256（原版用时间戳，同书转两次就是两本书，阅读进度会丢） |
| **非交互** | 新增 `--non-interactive` 与 `--metadata-file`，可在 CI / 批处理 / 容器内无人值守运行 |
| **其它** | 尊重调用方传入的 `output_path`；支持「只有 Markdown 没有 PDF」的目录；补 `pygments` 依赖声明 |

## 🛠️ 依赖

- Python 3.10–3.14（3.13 推荐）
- `marker-pdf==1.10.2`（版面还原；代码 Apache-2.0，模型权重为修改版 OpenRAIL-M，营收 < 500 万美元免费）
- `markdown` / `latex2mathml` / `Pygments`

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Apple Silicon 上 MPS 开箱可用；CUDA / ROCm 按 [pytorch.org](https://pytorch.org/get-started/locally/) 重装 torch。

## 🚀 用法

### 交互式（本地手动）

```bash
python main.py book.pdf                    # 单本
python main.py ./pdfs/                     # 整目录
```

EPUB 阶段会提示填写标题、作者、语言等元数据。

### 非交互（推荐）

```bash
python main.py book.pdf --non-interactive --metadata-file meta.json
```

`meta.json`：

```json
{
  "dc:title": "宪法（第六版）",
  "dc:creator": "［日］芦部信喜",
  "dc:language": "zh-CN",
  "dc:publisher": "清华大学出版社",
  "dc:date": "2018-08-01"
}
```

省略 `dc:language` 时会从 marker metadata 或正文 CJK 占比自动推断。
省略 `dc:identifier` 时自动生成基于内容的稳定 URN。

### 只重打包已有 Markdown

```bash
python main.py ./book_md --skip-md --non-interactive --metadata-file meta.json
```

目录里有 `*.md` 即可，不需要 PDF。

### 选项

```
--max-pages INT          最多处理多少页
--start-page INT         从第几页开始（marker 1.x 需同时给 --max-pages）
--skip-epub              只出 Markdown
--skip-md                复用已有 Markdown，只出 EPUB
--non-interactive        完全不读 stdin，全部用默认值 / metadata-file
--metadata-file PATH     指定元数据 JSON
```

## 📁 输出结构

```
book/
├── book.md                  # marker 的原始整本 Markdown（含 {页码} 分隔块）
├── book_metadata.json       # marker 输出的 pdf_toc / computed_toc / ocr_stats
├── chapters.json            # 切分后的章节清单（含层级）
├── 00.md 01.md 02.md ...    # 按章切分的 Markdown
├── images/                  # 提取的插图
├── description.json         # EPUB 元数据
└── book.epub
```

## ✅ 验收

仓库自带一个不依赖 Java / EPUBCheck 的校验脚本，覆盖 EPUBCheck 的关键规则：

```bash
python validate.py book/book.epub
```

检查项：mimetype 首项且未压缩、所有 XML 良构、container→OPF 可达、OPF 必填元数据
（title/identifier/language/`dcterms:modified`、`unique-identifier` 可解析）、manifest↔zip
一致且有 media-type、manifest id 无重复、nav 含 `toc`/`page-list` 且链接与锚点全部可达、
spine 引用有效、每个内容文档有 `<title>`。

## ⚠️ 已知限制

- `computed_toc` 是 marker **推断**的章节表；无书签的扫描书可能只推断出一两层。此时退化为按一级标题切分。
- 页边注、双栏索引等复杂版面仍可能错序（版面还原由 marker 决定，本项目不再加工）。
- 数学公式经 `latex2mathml` 转 MathML；部分阅读器对 MathML 支持不完整。

## 📄 许可证

MIT（继承上游）。marker-pdf 的代码为 Apache-2.0，模型权重另有 OpenRAIL-M 条款。
