"""按书籍结构把单个 Markdown 切成按章文件。

优先级：pdf_toc（PDF 原生书签） > computed_toc（marker 推断） > 一级标题兜底。
页码来自 marker 的 PAGINATE_OUTPUT 分隔符（形如 `{4}` 后接 48 个短横线），
用于生成 EPUB3 的 page-list。
"""
import re
import unicodedata
from pathlib import Path

# marker 的页码分隔块：空行 + {页码} + 48 个短横线 + 空行
PAGE_MARK = re.compile(r"\n*^\{(\d+)\}$\n-{10,}\n*", re.MULTILINE)
HANY = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _slug(text, idx, used):
    """生成稳定、ASCII 安全、可排序的文件名前缀。"""
    stem = f"{idx:02d}"
    name = stem
    k = 1
    while name in used:
        name = f"{stem}-{k}"
        k += 1
    used.add(name)
    return name


def _toc_titles(metadata):
    """从 marker metadata 里取章节标题列表，返回 [(level, title), ...]。"""
    for key in ("pdf_toc", "computed_toc"):
        raw = metadata.get(key) or []
        out = []
        for item in raw:
            title = (item.get("title") or "").strip() if isinstance(item, dict) else str(item).strip()
            if not title:
                continue
            lvl = item.get("level", 1) if isinstance(item, dict) else 1
            out.append((int(lvl), title))
        if out:
            return out, key
    return [], None


def split_by_headings(text):
    """兜底：按一级标题切分。返回 [(title, chunk_text)]。"""
    matches = list(H1.finditer(text))
    if len(matches) < 2:
        return []
    chunks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunks.append((m.group(1).strip(), text[start:end].strip()))
    # 第一个标题之前的正文单列为前言
    if matches[0].start() > 0:
        head = text[: matches[0].start()].strip()
        if head:
            chunks.insert(0, ("前言", head))
    return chunks


def split_markdown(md_text, metadata, out_dir):
    """把 md_text 切成多文件写到 out_dir，返回章节清单。

    返回 [{'file': 文件名, 'title': 标题, 'level': 层级}, ...]
    """
    out_dir = Path(out_dir)
    toc, source = _toc_titles(metadata)
    pages = PAGE_MARK.findall(md_text)   # 仅用于统计，正文里保留分隔符由 mark2epub 解析

    used = set()
    chapters = []
    if len(toc) >= 2:
        # 只按「编/章」级别切分；节（level 3）与更细条目留在所属章内，
        # 否则会切出大量只有标题、没有正文的碎片文件。
        toc = [(lvl, title) for lvl, title in toc if lvl <= 2]
        # 用 TOC 标题在正文中定位：逐条查找标题文字的首次出现位置
        positions = []
        cursor = 0
        for lvl, title in toc:
            probe = _norm(title)
            pos = -1
            best = None
            for m in HANY.finditer(md_text):
                if m.start() < cursor:
                    continue
                mt = _norm(m.group(2))
                if not (mt.startswith(probe[:12]) or probe[:12] in mt):
                    continue
                # 层级越接近越优先
                same = (len(m.group(1)) == lvl)
                if same:
                    best = m.start()
                    break
                if best is None:
                    best = m.start()
            pos = best if best is not None else -1
            if pos < 0:
                continue
            positions.append((pos, lvl, title))
            cursor = pos + 1
        if positions:
            for i, (pos, lvl, title) in enumerate(positions):
                end = positions[i + 1][0] if i + 1 < len(positions) else len(md_text)
                body = md_text[pos:end].strip()
                name = _slug(title, i, used) + ".md"
                (out_dir / name).write_text(body, encoding="utf-8")
                chapters.append({"file": name, "title": title, "level": lvl, "source": source})
            _write_chapter_index(out_dir, chapters)
            _archive_source(out_dir)
            # 首个标题之前的内容（封面文案、前言等）单独成篇
            if positions[0][0] > 0:
                head = md_text[: positions[0][0]].strip()
                if head:
                    name = "00-frontmatter.md"
                    (out_dir / name).write_text(head, encoding="utf-8")
                    chapters.insert(0, {"file": name, "title": "前言", "level": 1, "source": "head"})
            _write_chapter_index(out_dir, chapters)
            _archive_source(out_dir)
            return chapters, len(pages)

    # 兜底：按一级标题切
    for i, (title, body) in enumerate(split_by_headings(md_text)):
        name = _slug(title, i, used) + ".md"
        (out_dir / name).write_text(body, encoding="utf-8")
        chapters.append({"file": name, "title": title, "level": 1, "source": "heading"})
    _write_chapter_index(out_dir, chapters)
    return chapters, len(pages)


def _archive_source(out_dir):
    """把切分前的整本 markdown 改名为 *.md.full，避免被 mark2epub 当成章节。"""
    out_dir = Path(out_dir)
    for f in list(out_dir.glob("*.md")):
        # 章节文件都是 NN.md / NN-标题.md 形态，其余视为原始整本
        if not _chap_name(f.name):
            try:
                f.rename(f.with_suffix(".md.full"))
            except Exception:
                pass


def _chap_name(name):
    import re as _re
    return bool(_re.match(r"^(\d{2}(-\d+)?|00-frontmatter)\.md$", name))


def _write_chapter_index(out_dir, chapters):
    """把章节清单（含 level）落盘，mark2epub 据此生成层级目录。"""
    import json as _json
    out_dir = Path(out_dir)
    data = [{"file": c["file"], "title": c["title"], "level": c.get("level", 1)} for c in chapters]
    (out_dir / "chapters.json").write_text(
        _json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s\u3000·•、,，.。:：;；\-—–_]+", "", s)
