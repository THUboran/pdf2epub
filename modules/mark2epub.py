import markdown
import os
from xml.dom import minidom
import zipfile
import sys
import json
from PIL import Image
import regex as re
from pathlib import Path
from datetime import datetime, timezone
import subprocess
from typing import Dict, Optional
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape
import latex2mathml.converter


PAGE_MARK = re.compile(r"\n*^\{(\d+)\}$\n-{10,}\n*", re.MULTILINE)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _infer_language(work_dir, markdown_dir=None):
    """从 *​_metadata.json 或正文 CURRENCY 字符推断语言；失败返回 None。"""
    import glob as _glob
    try:
        for mf in sorted(_glob.glob(os.path.join(work_dir, "*_metadata.json"))):
            try:
                with open(mf, "r", encoding="utf-8") as f:
                    m = json.load(f)
            except Exception:
                continue
            langs = m.get("languages")
            if isinstance(langs, list) and langs:
                return _map_lang(langs[0])
            if isinstance(langs, str) and langs.strip():
                return _map_lang(langs.strip())
    except Exception:
        pass
    # 按正文字符集兜底：CJK 占比高 → zh-CN
    try:
        sample = ""
        for md in sorted(_glob.glob(os.path.join(work_dir, "*.md")))[:2]:
            sample += Path(md).read_text(encoding="utf-8")[:2000]
        if sample:
            cjk = sum(1 for ch in sample if '\u4e00' <= ch <= '\u9fff')
            if cjk > len(sample) * 0.15:
                return "zh-CN"
            return "en"
    except Exception:
        pass
    return None


_LANG_MAP = {
    "chinese": "zh-CN", "simplified chinese": "zh-CN", "zh": "zh-CN",
    "traditional chinese": "zh-TW", "english": "en", "japanese": "ja",
    "korean": "ko", "french": "fr", "german": "de", "spanish": "es",
    "russian": "ru", "italian": "it", "portuguese": "pt", "arabic": "ar",
}


def _map_lang(name):
    key = (name or "").strip().lower()
    if key in _LANG_MAP:
        return _LANG_MAP[key]
    # 已经是 BCP-47 形式
    if re.fullmatch(r"[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*", key):
        return key
    return None


def _content_identifier(work_dir):
    """基于正文字节内容生成稳定 identifier（同内容不同次运行结果相同）。"""
    import hashlib
    h = hashlib.sha256()
    files = sorted(f for f in os.listdir(work_dir) if f.endswith(".md"))
    for name in files:
        with open(os.path.join(work_dir, name), "rb") as f:
            h.update(name.encode("utf-8"))
            h.update(f.read())
    return "urn:sha256:" + h.hexdigest()

def get_user_input(prompt: str, default: str = "") -> str:
    """Get user input with a default value."""
    user_input = input(f"{prompt} [{default}]: ").strip()
    return user_input if user_input else default

def get_metadata_from_user(existing_metadata: Optional[Dict] = None, non_interactive: bool = False) -> Dict:
    """Interactively collect metadata from user with defaults from existing metadata.

    non_interactive=True 时全部使用默认值/已有值，不读 stdin（供批处理与 CI）。
    """
    if existing_metadata is None:
        existing_metadata = {}
    
    metadata = existing_metadata.get("metadata", {})
    
    print("\nPlease provide the following metadata for your EPUB (press Enter to use default value):")
    
    fields = {
        "dc:title": ("Title", metadata.get("dc:title", "Untitled Document")),
        "dc:creator": ("Author(s)", metadata.get("dc:creator", "Unknown Author")),
        "dc:identifier": ("Unique Identifier", metadata.get("dc:identifier", f"id-{datetime.now().strftime('%Y%m%d%H%M%S')}")),
        "dc:language": ("Language (e.g., en, de, fr)", metadata.get("dc:language", "en")),
        "dc:rights": ("Rights", metadata.get("dc:rights", "All rights reserved")),
        "dc:publisher": ("Publisher", metadata.get("dc:publisher", "PDF2EPUB")),
        "dc:date": ("Publication Date (YYYY-MM-DD)", metadata.get("dc:date", datetime.now().strftime("%Y-%m-%d")))
    }
    
    updated_metadata = {}
    for key, (prompt, default) in fields.items():
        value = default if non_interactive else get_user_input(prompt, default)
        updated_metadata[key] = value
        
    return {
        "metadata": updated_metadata,
        "default_css": existing_metadata.get("default_css", ["style.css"]),
        "chapters": existing_metadata.get("chapters", []),
        "cover_image": existing_metadata.get("cover_image", None)
    }

def review_markdown(markdown_path: Path) -> tuple[bool, str]:
    """Ask user if they want to review the markdown file."""
    content = markdown_path.read_text(encoding='utf-8')
    
    if os.environ.get("PDF2EPUB_NONINTERACTIVE") == "1":
        return True, content
    while True:
        response = input("\nWould you like to review the markdown file before conversion? (y/n): ").lower()
        if response in ['y', 'yes']:
            try:
                if sys.platform == 'darwin':
                    subprocess.run(['open', str(markdown_path)], check=True)
                elif os.name == 'posix':
                    subprocess.run(['xdg-open', str(markdown_path)], check=True)
                else:
                    os.startfile(str(markdown_path))
                
                while True:
                    proceed = input("\nPress Enter when you're done editing (or 'q' to abort): ").lower()
                    if proceed == 'q':
                        return False, content
                    elif proceed == '':
                        updated_content = markdown_path.read_text(encoding='utf-8')
                        return True, updated_content
            except Exception as e:
                print(f"\nError opening markdown file: {e}")
                print("Proceeding with conversion...")
                return True, content
        elif response in ['n', 'no']:
            return True, content
        else:
            print("Please enter 'y' or 'n'")

def build_image_lookup(images_dir: Path) -> dict:
    """Build a {lowercase_name: actual_path} map for O(1) case-insensitive lookups."""
    lookup = {}
    try:
        for entry in images_dir.iterdir():
            lookup[entry.name.lower()] = entry
    except FileNotFoundError:
        pass
    return lookup

def process_markdown_for_images(markdown_text: str, work_dir: Path) -> tuple[str, list[str]]:
    """Process markdown content to find image references."""
    image_pattern = r'!\[(.*?)\]\((.*?)\)'
    images_found = []
    modified_text = markdown_text
    images_dir = work_dir / 'images'
    lookup = build_image_lookup(images_dir)

    for match in re.finditer(image_pattern, markdown_text):
        alt_text, image_path = match.groups()
        img_path = Path(image_path.strip())

        actual = lookup.get(img_path.name.lower())
        if actual is not None:
            images_found.append(actual.name)
            new_ref = f'![{alt_text}](images/{actual.name})'
            modified_text = modified_text.replace(match.group(0), new_ref)
        else:
            print(f"Warning: Image not found: {images_dir / img_path.name}")

    return modified_text, images_found

def copy_and_optimize_image(src_path: Path, dest_path: Path, max_dimension: int = 2400) -> Path:
    """Copy image to destination path with optimization for EPUB.

    返回实际写入的路径（非 jpg/png 会转成 jpg，扩展名因此变化）。
    """
    try:
        with Image.open(src_path) as img:
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')

            ratio = min(max_dimension / max(img.size[0], img.size[1]), 1.0)
            if ratio < 1.0:
                new_size = tuple(int(dim * ratio) for dim in img.size)
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            ext = src_path.suffix.lower()
            if ext in ('.jpg', '.jpeg'):
                img.save(dest_path, 'JPEG', quality=85, optimize=True)
            elif ext == '.png':
                img.save(dest_path, 'PNG', optimize=True)
            else:
                # tif/tiff/bmp/webp 等统一转 jpg
                dest_path = dest_path.with_suffix('.jpg')
                img.save(dest_path, 'JPEG', quality=85, optimize=True)
        return dest_path
    except Exception as e:
        print(f"Error processing image {src_path}: {e}")
        raise

def get_all_filenames(the_dir, extensions=[]):
    if not os.path.exists(the_dir):
        return []
    all_files = [x for x in os.listdir(the_dir)]
    all_files = [x for x in all_files if x.split(".")[-1] in extensions]
    return all_files

def _nested_ol(chapters):
    """按 level 生成合法的嵌套有序列表。

    做法：递归建树再渲染，避免手工闭合标签出错。
    """
    nodes = []          # 树：{'ch': ch, 'idx': i, 'children': []}
    stack = []          # 从根到当前的节点栈
    for i, ch in enumerate(chapters):
        lvl = max(1, int(ch.get("level", 1)))
        node = {"ch": ch, "idx": i, "lvl": lvl, "children": []}
        while stack and stack[-1]["lvl"] >= lvl:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            nodes.append(node)
        stack.append(node)

    def render(ns):
        out = ["<ol>"]
        for n in ns:
            out.append('<li><a href="{}">{}</a>'.format(
                quote(_chapter_href(n["ch"], n["idx"])), xml_escape(n["ch"]["title"])))
            if n["children"]:
                out.append(render(n["children"]))
            out.append("</li>")
        out.append("</ol>")
        return "".join(out)

    return render(nodes)

def get_packageOPF_XML(chapters=None, image_filenames=[], css_filenames=[], description_data=None, page_files=None):
    doc = minidom.Document()

    package = doc.createElement('package')
    package.setAttribute('xmlns',"http://www.idpf.org/2007/opf")
    package.setAttribute('version',"3.0")
    _lang = description_data["metadata"].get("dc:language", "zh-CN") if description_data else "zh-CN"
    package.setAttribute('xml:lang', _lang)
    package.setAttribute("unique-identifier","pub-id")

    ## Now building the metadata

    metadata = doc.createElement('metadata')
    metadata.setAttribute('xmlns:dc', 'http://purl.org/dc/elements/1.1/')

    for k,v in description_data["metadata"].items():
        if len(v):
            x = doc.createElement(k)
            for metadata_type,id_label in [("dc:title","title"),("dc:creator","creator"),("dc:identifier","pub-id")]:
                if k==metadata_type:
                    x.setAttribute('id',id_label)
            x.appendChild(doc.createTextNode(v))
            metadata.appendChild(x)

    # Required by EPUB 3: dcterms:modified timestamp
    modified_meta = doc.createElement('meta')
    modified_meta.setAttribute('property', 'dcterms:modified')
    modified_meta.appendChild(doc.createTextNode(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    metadata.appendChild(modified_meta)


    ## Now building the manifest

    manifest = doc.createElement('manifest')

    ## TOC.xhtml file for EPUB 3
    x = doc.createElement('item')
    x.setAttribute('id',"toc")
    x.setAttribute('properties',"nav")
    x.setAttribute('href',"TOC.xhtml")
    x.setAttribute('media-type',"application/xhtml+xml")
    manifest.appendChild(x)

    ## Ensure retrocompatibility by also providing a TOC.ncx file
    x = doc.createElement('item')
    x.setAttribute('id',"ncx")
    x.setAttribute('href',"toc.ncx")
    x.setAttribute('media-type',"application/x-dtbncx+xml")
    manifest.appendChild(x)

    x = doc.createElement('item')
    x.setAttribute('id',"titlepage")
    x.setAttribute('href',"titlepage.xhtml")
    x.setAttribute('media-type',"application/xhtml+xml")
    manifest.appendChild(x)

    for i, ch in enumerate(chapters):
        x = doc.createElement('item')
        x.setAttribute('id',"s{:05d}".format(i))
        x.setAttribute('href', quote(_chapter_href(ch, i)))
        x.setAttribute('media-type',"application/xhtml+xml")
        manifest.appendChild(x)

    for i,image_filename in enumerate(image_filenames):
        x = doc.createElement('item')
        x.setAttribute('id',"image-{:05d}".format(i))
        x.setAttribute('href', "images/{}".format(quote(image_filename)))
        ext = Path(image_filename).suffix.lower()
        if ext == '.gif':
            x.setAttribute('media-type',"image/gif")
        elif ext in ['.jpg', '.jpeg']:
            x.setAttribute('media-type',"image/jpeg")
        elif ext == '.png':
            x.setAttribute('media-type',"image/png")
        if image_filename==description_data["cover_image"]:
            x.setAttribute('properties',"cover-image")

            ## Ensure compatibility by also providing a meta tag in the metadata
            y = doc.createElement('meta')
            y.setAttribute('name',"cover")
            y.setAttribute('content',"image-{:05d}".format(i))
            metadata.appendChild(y)
        manifest.appendChild(x)

    for i,css_filename in enumerate(css_filenames):
        x = doc.createElement('item')
        x.setAttribute('id',"css-{:05d}".format(i))
        x.setAttribute('href',"css/{}".format(css_filename))
        x.setAttribute('media-type',"text/css")
        manifest.appendChild(x)

    ## Now building the spine

    spine = doc.createElement('spine')
    spine.setAttribute('toc', "ncx")

    x = doc.createElement('itemref')
    x.setAttribute('idref',"titlepage")
    x.setAttribute('linear',"yes")
    spine.appendChild(x)
    for i, _ in enumerate(chapters):
        x = doc.createElement('itemref')
        x.setAttribute('idref',"s{:05d}".format(i))
        x.setAttribute('linear',"yes")
        spine.appendChild(x)

    guide = doc.createElement('guide')
    x = doc.createElement('reference')
    x.setAttribute('type',"cover")
    x.setAttribute('title',"Cover image")
    x.setAttribute('href',"titlepage.xhtml")
    guide.appendChild(x)


    package.appendChild(metadata)
    package.appendChild(manifest)
    package.appendChild(spine)
    package.appendChild(guide)
    doc.appendChild(package)

    return doc.toprettyxml()


def get_container_XML():
    container_data = """<?xml version="1.0" encoding="UTF-8" ?>\n"""
    container_data += """<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n"""
    container_data += """<rootfiles>\n"""
    container_data += """<rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/>\n"""
    container_data += """</rootfiles>\n</container>"""
    return container_data

def get_coverpage_XML(title, authors, lang="zh-CN"):
    """Generate a simple cover page with title and optional author input."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{xml_escape(lang)}">
<head>
<title>Cover Page</title>
<style type="text/css">
body {{ 
    margin: 0;
    padding: 0;
    height: 100vh;
    display: flex;
    justify-content: center;
    align-items: center;
    font-family: serif;
}}
.cover {{
    padding: 3em;
    text-align: center;
    border: 1px solid #ccc;
    max-width: 80%;
}}
h1 {{
    font-size: 2em;
    margin-bottom: 1em;
    line-height: 1.2;
    color: #333;
}}
p {{
    font-size: 1.2em;
    font-style: italic;
    color: #666;
    line-height: 1.4;
}}
</style>
</head>
<body>
    <div class="cover">
        <h1>{xml_escape(title)}</h1>
        <p>{xml_escape(authors) if authors else ''}</p>
    </div>
</body>
</html>"""

def get_TOC_XML(default_css_filenames, chapters, lang="zh-CN", page_files=None):
    """生成 nav.xhtml。chapters: [{file,title,level,...}]；page_files: [(页码, href)] 可选。"""
    toc_xhtml = """<?xml version="1.0" encoding="UTF-8"?>\n"""
    toc_xhtml += """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{}">\n""".format(xml_escape(lang))
    toc_xhtml += """<head>\n<meta http-equiv="default-style" content="text/html; charset=utf-8"/>\n"""
    toc_xhtml += """<title>目录</title>\n"""
    for css_filename in default_css_filenames:
        toc_xhtml += """<link rel="stylesheet" href="css/{}" type="text/css"/>\n""".format(css_filename)
    toc_xhtml += """</head>\n<body>\n"""

    # 目录：按 level 生成嵌套 <ol>
    toc_xhtml += """<nav epub:type="toc" role="doc-toc" id="toc">\n<h2>目录</h2>\n"""
    toc_xhtml += _nested_ol(chapters)
    toc_xhtml += """</nav>\n"""

    # 地标
    toc_xhtml += """<nav epub:type="landmarks" hidden="">\n<ol>\n"""
    toc_xhtml += """<li><a epub:type="bodymatter" href="{}">{}</a></li>\n""".format(
        quote(_chapter_href(chapters[0], 0)) if chapters else "titlepage.xhtml",
        xml_escape(chapters[0]["title"]) if chapters else "正文")
    toc_xhtml += """</ol>\n</nav>\n"""

    # 页码列表
    if page_files:
        toc_xhtml += """<nav epub:type="page-list" hidden="">\n<ol>\n"""
        for pno, href in page_files:
            toc_xhtml += """<li><a href="{}">{}</a></li>\n""".format(_href_with_anchor(href), pno)
        toc_xhtml += """</ol>\n</nav>\n"""

    toc_xhtml += """</body>\n</html>"""
    return toc_xhtml


def _chapter_href(ch, idx):
    return "s{:05d}-{}.xhtml".format(idx, ch["file"].split(".")[0])


def _href_with_anchor(href):
    """quote 路径但保留 # 锚点（quote 会把 # 编成 %23 导致锚点失效）。"""
    path, _, anchor = href.partition("#")
    out = quote(path)
    return out + ("#" + anchor if anchor else "")


def get_TOCNCX_XML(chapters, uid="", title=""):
    ## Returns the XML data for the TOC.ncx file

    toc_ncx = """<?xml version="1.0" encoding="UTF-8"?>\n"""
    toc_ncx += """<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n"""
    toc_ncx += """<head>\n"""
    toc_ncx += """<meta name="dtb:uid" content="{}"/>\n""".format(xml_escape(uid))
    toc_ncx += """<meta name="dtb:depth" content="2"/>\n"""
    toc_ncx += """<meta name="dtb:totalPageCount" content="0"/>\n"""
    toc_ncx += """<meta name="dtb:maxPageNumber" content="0"/>\n"""
    toc_ncx += """</head>\n"""
    toc_ncx += """<docTitle><text>{}</text></docTitle>\n""".format(xml_escape(title))
    toc_ncx += """<navMap>\n"""
    for i, ch in enumerate(chapters):
        src = quote(_chapter_href(ch, i))
        toc_ncx += """<navPoint id="navpoint-{}" playOrder="{}">\n""".format(i, i + 1)
        toc_ncx += """<navLabel>\n<text>{}</text>\n</navLabel>""".format(xml_escape(ch["title"]))
        toc_ncx += """<content src="{}"/>""".format(src)
        toc_ncx += """ </navPoint>"""
    toc_ncx += """</navMap>\n</ncx>"""

    return toc_ncx

def convert_math_to_mathml(html_text: str) -> str:
    """Replace LaTeX math expressions in HTML with MathML, skipping code blocks."""
    # Mask <pre>/<code> blocks so their $ delimiters are never treated as math
    placeholders = {}
    counter = [0]

    def mask(m):
        key = f"\x00MASK{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = m.group(0)
        return key

    masked = re.sub(r'<pre[\s\S]*?</pre>|<code[\s\S]*?</code>', mask, html_text, flags=re.DOTALL)

    def try_convert(latex):
        try:
            out = latex2mathml.converter.convert(latex)
        except Exception:
            return None
        # latex2mathml 不转义正文里的 &，会产出非法 XHTML（xmlParseEntityRef）
        return re.sub(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,10}|#\d{1,6}|#x[0-9a-fA-F]{1,5});)",
                      "&amp;", out)

    # Standalone display math that Markdown wrapped in <p>: replace the whole
    # paragraph to avoid invalid XHTML like <p><div>...</div></p>
    def replace_display_paragraph(m):
        mathml = try_convert(m.group(1))
        return f'<div class="math-display">{mathml}</div>' if mathml else m.group(0)

    # Remaining $$...$$ (inside phrasing content): use <span> to stay valid
    def replace_display_inline(m):
        mathml = try_convert(m.group(1))
        return f'<span class="math-display">{mathml}</span>' if mathml else m.group(0)

    # Inline $...$
    def replace_inline(m):
        mathml = try_convert(m.group(1))
        return f'<span class="math-inline">{mathml}</span>' if mathml else m.group(0)

    masked = re.sub(r'<p>\s*\$\$(.*?)\$\$\s*</p>', replace_display_paragraph, masked, flags=re.DOTALL)
    masked = re.sub(r'\$\$(.*?)\$\$', replace_display_inline, masked, flags=re.DOTALL)
    masked = re.sub(r'(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)', replace_inline, masked, flags=re.DOTALL)

    for key, original in placeholders.items():
        masked = masked.replace(key, original)

    return masked

def get_chapter_XML(work_dir: str, md_filename: str, css_filenames: list[str], content: Optional[str] = None, lang: str = "zh-CN", chapter_title: Optional[str] = None) -> tuple[str, list[str], list[int]]:
    """
    Convert markdown chapter to XHTML and process images.
    Returns tuple of (XHTML content, list of images referenced in chapter)
    
    Args:
        work_dir: Working directory containing markdown files
        md_filename: Name of markdown file
        css_filenames: List of CSS files to include
        content: Optional pre-loaded markdown content. If None, content is read from file
    """
    work_dir_path = Path(work_dir)
    
    if content is None:
        with open(work_dir_path / md_filename, "r", encoding="utf-8") as f:
            markdown_data = f.read()
    else:
        markdown_data = content

    # 页码分隔块 → 章内锚点（必须在 Markdown 解析之前替换，
    # 否则 marker 的 "{3}\n-----" 会被 setext 规则吃成 <h2>{3}</h2>）
    raw_pages = PAGE_MARK.findall(markdown_data)
    markdown_data = PAGE_MARK.sub(
        lambda mm: '\n\n<div class="pagebreak" id="page-%s"></div>\n\n' % mm.group(1),
        markdown_data)

    # Process markdown for images and get list of referenced images
    markdown_data, chapter_images = process_markdown_for_images(markdown_data, work_dir_path)
    
    # 裸 & 会让 XHTML 非法（xmlParseEntityRef）。
    # Python-Markdown 假定输入已是合法 HTML，不会转义正文里的 &，故先做最小转义：
    # 只转换不属于既有实体的 &，避免破坏 &amp; &#123; 等合法写法。
    markdown_data = re.sub(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,10}|#\d{1,6}|#x[0-9a-fA-F]{1,5});)",
                           "&amp;", markdown_data)

    # Convert to HTML
    html_text = markdown.markdown(
        markdown_data,
        extensions=["codehilite", "tables", "fenced_code", "footnotes"],
        extension_configs={"codehilite": {"guess_lang": False}}
    )

    # Convert LaTeX math to MathML for EPUB readers
    html_text = convert_math_to_mathml(html_text)

    # 章节标题：优先调用方传入（来自 TOC），其次首个一级标题，最后文件名
    title = chapter_title
    if not title:
        m = H1_RE.search(markdown_data)
        title = m.group(1).strip() if m else Path(md_filename).stem

    # Generate XHTML wrapper —— 必须带 <title>，否则 EPUBCheck 报 RSC-005
    xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xmlns:m="http://www.w3.org/1998/Math/MathML" lang="{xml_escape(lang)}" xml:lang="{xml_escape(lang)}">
<head>
    <meta http-equiv="default-style" content="text/html; charset=utf-8"/>
    <title>{xml_escape(title)}</title>
    {''.join(f'<link rel="stylesheet" href="css/{css}" type="text/css" media="all"/>' for css in css_filenames)}
</head>
<body>
{html_text}
</body>
</html>"""

    return xhtml, chapter_images, [int(p) for p in raw_pages]



def convert_to_epub(markdown_dir: Path, output_path: Path) -> None:
    """
    Convert markdown files and images to EPUB format.
    """
    if not markdown_dir.exists():
        raise FileNotFoundError(f"Markdown directory not found: {markdown_dir}")
        
    if not list(markdown_dir.glob('*.md')):
        raise ValueError(f"No markdown files found in: {markdown_dir}")
    
    # Generate EPUB file —— 尊重调用方传入的 output_path。
    # output_path 可能是「目录」也可能是「文件」：目录则在其下按书名命名。
    md_dir = Path(markdown_dir)
    if output_path is None:
        epub_path = md_dir / f"{md_dir.name}.epub"
    else:
        op = Path(output_path)
        if op.suffix.lower() == ".epub":
            epub_path = op
        else:
            op.mkdir(parents=True, exist_ok=True)
            epub_path = op / f"{md_dir.name}.epub"
    main([str(md_dir), str(epub_path)])

def main(args):
    if len(args) < 2:
        print("\nUsage:\n    python md2epub.py <markdown_directory> <output_file.epub>")
        exit(1)

    work_dir = args[0]
    output_path = args[1]

    images_dir = os.path.join(work_dir, 'images/')
    css_dir = os.path.join(work_dir, 'css/')

    try:
        # Reading/Creating the JSON file containing the description of the eBook
        description_path = os.path.join(work_dir, "description.json")
        existing_metadata = {}
        
        if os.path.exists(description_path):
            with open(description_path, 'r', encoding='utf-8') as f:
                existing_metadata = json.load(f)
        
        # Get metadata from user
        json_data = get_metadata_from_user(
            existing_metadata,
            non_interactive=(os.environ.get("PDF2EPUB_NONINTERACTIVE") == "1"),
        )

        # 语言：用户输入优先；否则从书籍 metadata/字符集推断；最后兜底 zh-CN
        # 注意：必须回写进 metadata，否则 OPF 里 dc:language 会被跳过（EPUBCheck 必填项）
        lang = (json_data["metadata"].get("dc:language") or "").strip()
        if not lang:
            lang = _infer_language(work_dir) or "zh-CN"
            json_data["metadata"]["dc:language"] = lang

        # 稳定 identifier：若用户没给（默认是时间戳），改内容哈希
        ident = (json_data["metadata"].get("dc:identifier") or "").strip()
        if not ident or ident.startswith("id-2"):
            json_data["metadata"]["dc:identifier"] = _content_identifier(work_dir)

        # 章节清单：优先使用 split_markdown 产出的顺序与标题
        if not json_data["chapters"]:
            markdown_files = [f for f in os.listdir(work_dir) if f.endswith('.md')]
            for md_file in sorted(markdown_files):
                json_data["chapters"].append({
                    "markdown": md_file,
                    "css": ""
                })
        
        # Save the updated description.json
        with open(description_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, indent=2)
        
        # Review markdown files and store updated content
        chapter_contents = {}
        for chapter in json_data["chapters"]:
            md_path = Path(work_dir) / chapter["markdown"]
            should_continue, content = review_markdown(md_path)
            if not should_continue:
                print("\nConversion aborted by user.")
                return
            chapter_contents[chapter["markdown"]] = content

        # Get title and author
        title = json_data["metadata"].get("dc:title", "Untitled Document")
        authors = json_data["metadata"].get("dc:creator", None)

        # Compile list of CSS files
        all_css_filenames = json_data["default_css"][:]
        for chapter in json_data["chapters"]:
            if len(chapter["css"]) and (chapter["css"] not in all_css_filenames):
                all_css_filenames.append(chapter["css"])
        
        all_image_filenames = get_all_filenames(images_dir, extensions=["gif", "jpg", "jpeg", "png"])

        # First process all chapters and images
        images_dir = Path(work_dir) / 'images'
        epub_images_dir = Path(work_dir) / 'epub_images'
        processed_images = {}  # Store processed image data（key = 实际写入文件名）
        image_renames = {}    # 原引用名 → 实际文件名（扩展名被转换时）
        all_referenced_images = set()
        chapter_data = {}  # Store processed chapter data
        chapters_meta = {}  # 有序章节元信息（用于目录/页码），key=i
        all_page_files = []  # [(页码, href#anchor)] 供 EPUB3 page-list

        # 读取 split_markdown 落盘的章节清单（含层级）；key 用文件名
        _chapter_index = {}
        _ci_path = os.path.join(work_dir, "chapters.json")
        if os.path.exists(_ci_path):
            try:
                with open(_ci_path, "r", encoding="utf-8") as f:
                    for _c in json.load(f):
                        _chapter_index[_c["file"]] = _c
            except Exception as _e:
                print(f"Warning: failed to read chapters.json: {_e}", file=sys.stderr)

        # First pass: Process chapters and collect image references
        print("\nProcessing chapters and collecting image references...")
        for i, chapter in enumerate(json_data["chapters"]):
            css_files = json_data["default_css"][:]
            if chapter["css"]:
                css_files.append(chapter["css"])
                
            # 章节标题：取本章首个一级标题（split_markdown 保持它在首行）
            chapter_title = None
            m = H1_RE.search(chapter_contents[chapter["markdown"]])
            if m:
                chapter_title = m.group(1).strip()
            chapter_xhtml, chapter_images, chapter_pages = get_chapter_XML(
                work_dir, 
                chapter["markdown"], 
                css_files,
                content=chapter_contents[chapter["markdown"]],
                lang=lang,
                chapter_title=chapter_title,
            )
            chapter_data[chapter["markdown"]] = chapter_xhtml
            _ci = _chapter_index.get(chapter["markdown"], {})
            chapters_meta[i] = {
                "file": chapter["markdown"],
                "title": _ci.get("title") or chapter_title or Path(chapter["markdown"]).stem,
                "level": int(_ci.get("level", 1)),
            }
            for pno in chapter_pages:
                all_page_files.append((pno, _chapter_href(chapters_meta[i], i) + "#page-%d" % pno))
            all_referenced_images.update(chapter_images)

        # Process and optimize images
        print("\nProcessing and optimizing images...")
        if images_dir.exists() and all_referenced_images:
            epub_images_dir.mkdir(exist_ok=True)
            
            for image in all_referenced_images:
                src_path = images_dir / image
                if src_path.exists():
                    try:
                        # copy_and_optimize_image 对非 jpg/png 会改后缀，
                        # 因此必须用它的返回值作为实际写入路径。
                        dest_path = epub_images_dir / image
                        real_path = copy_and_optimize_image(src_path, dest_path)
                        if real_path is None or not Path(real_path).exists():
                            raise FileNotFoundError(f"optimize failed for {image}")
                        actual_name = Path(real_path).name
                        with open(real_path, "rb") as f:
                            processed_images[actual_name] = f.read()
                        if actual_name != image:
                            image_renames[image] = actual_name
                    except Exception as e:
                        print(f"Warning: Failed to process image {image}: {e}")
                else:
                    print(f"Warning: Referenced image not found: {src_path}")
            
            # Cleanup temporary directory
            import shutil
            shutil.rmtree(epub_images_dir, ignore_errors=True)

        # 图片扩展名被转换时，把章节里的引用同步改掉
        if image_renames:
            for k in list(chapter_data.keys()):
                txt = chapter_data[k]
                for old_name, new_name in image_renames.items():
                    txt = txt.replace(f"images/{old_name}", f"images/{new_name}")
                chapter_data[k] = txt

        # Now create the EPUB file with all prepared content
        print("\nCreating EPUB file...")
        with zipfile.ZipFile(output_path, "w") as epub:
            # Write mimetype (must be first and uncompressed)
            epub.writestr("mimetype", "application/epub+zip")

            # Write container.xml
            epub.writestr("META-INF/container.xml", get_container_XML(), zipfile.ZIP_DEFLATED)

            # 有序章节清单（按 json_data["chapters"] 顺序）
            ch_list = [chapters_meta[i] for i in range(len(json_data["chapters"]))]

            # Write package.opf
            epub.writestr("OPS/package.opf", 
                get_packageOPF_XML(
                    chapters=ch_list,
                    image_filenames=sorted(processed_images.keys()) + sorted(
                        set(all_image_filenames) - set(processed_images.keys())),
                    css_filenames=all_css_filenames,
                    description_data=json_data,
                    page_files=all_page_files,
                ), 
                zipfile.ZIP_DEFLATED
            )

            # Write cover page
            coverpage_data = get_coverpage_XML(title, authors, lang=lang)
            epub.writestr("OPS/titlepage.xhtml", coverpage_data.encode('utf-8'), zipfile.ZIP_DEFLATED)

            # Write processed chapters
            print("Writing chapters...")
            for i, ch in enumerate(ch_list):
                print(f"  Writing chapter {i+1}/{len(ch_list)}: {ch['title']}")
                epub.writestr(
                    "OPS/" + _chapter_href(ch, i),
                    chapter_data[ch["file"]].encode('utf-8'),
                    zipfile.ZIP_DEFLATED
                )

            # Write processed images
            if processed_images:
                print(f"Writing {len(processed_images)} processed images...")
                for image_name, image_data in processed_images.items():
                    epub.writestr(f"OPS/images/{image_name}", image_data, zipfile.ZIP_DEFLATED)

            # Write TOC files
            print("Writing table of contents...")
            epub.writestr("OPS/TOC.xhtml", 
                get_TOC_XML(json_data["default_css"], ch_list, lang=lang, page_files=all_page_files),
                zipfile.ZIP_DEFLATED
            )
            
            epub.writestr("OPS/toc.ncx",
                get_TOCNCX_XML(
                    ch_list,
                    uid=json_data["metadata"].get("dc:identifier", ""),
                    title=json_data["metadata"].get("dc:title", "")
                ),
                zipfile.ZIP_DEFLATED
            )

            # Copy remaining images that weren't referenced in markdown
            remaining_images = set(all_image_filenames) - set(processed_images.keys())
            if remaining_images and os.path.exists(images_dir):
                print(f"Writing {len(remaining_images)} additional images...")
                for image in remaining_images:
                    with open(os.path.join(images_dir, image), "rb") as f:
                        epub.writestr(f"OPS/images/{image}", f.read(), zipfile.ZIP_DEFLATED)

            # Copy CSS files; write a default style.css for any that are missing
            default_css_content = b"""body { font-family: serif; line-height: 1.5; margin: 5%; }
h1, h2, h3, h4, h5, h6 { font-family: sans-serif; }
img { max-width: 100%; height: auto; }
pre, code { font-family: monospace; font-size: 0.9em; }
"""
            print(f"Writing {len(all_css_filenames)} CSS files...")
            for css in all_css_filenames:
                css_path = os.path.join(css_dir, css)
                if os.path.exists(css_path):
                    with open(css_path, "rb") as f:
                        epub.writestr(f"OPS/css/{css}", f.read(), zipfile.ZIP_DEFLATED)
                else:
                    epub.writestr(f"OPS/css/{css}", default_css_content, zipfile.ZIP_DEFLATED)

        print(f"\nEPUB creation complete: {output_path}")
        
    except Exception:
        import traceback
        print(f"Error processing {work_dir}:")
        print(traceback.format_exc())
        raise

if __name__ == "__main__":
    main(sys.argv[1:])
