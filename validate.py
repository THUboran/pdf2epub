"""EPUBCheck 替代验证：良构性 + 关键结构规则。"""
import sys, zipfile
from urllib.parse import unquote
from lxml import etree

path = sys.argv[1]
z = zipfile.ZipFile(path)
names = set(z.namelist())
errors, warns = [], []

def err(m): errors.append(m)
def warn(m): warns.append(m)

# 1) mimetype
info = z.infolist()[0]
if info.filename != "mimetype": err("mimetype 不是第一项")
if info.compress_type != 0: err("mimetype 被压缩")
if z.read("mimetype") != b"application/epub+zip": err("mimetype 内容错")

# 2) 所有 XML/XHTML 良构
XHTML = "{http://www.w3.org/1999/xhtml}"
for n in names:
    if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
        try:
            etree.fromstring(z.read(n))
        except etree.XMLSyntaxError as e:
            err(f"{n}: XML 不良构 -> {e}")

# 3) container.xml 指向的 opf 存在
c = etree.fromstring(z.read("META-INF/container.xml"))
rf = c.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
opf_path = rf.get("full-path")
if opf_path not in names: err(f"container 指向的 {opf_path} 不存在")

# 4) OPF 必填项
NS = {"o": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
opf = etree.fromstring(z.read(opf_path))
for tag in ["title", "identifier", "language"]:
    if opf.find(f".//dc:{tag}", NS) is None:
        err(f"OPF 缺必填 dc:{tag}")
if opf.find(".//o:meta[@property='dcterms:modified']", NS) is None:
    err("OPF 缺 dcterms:modified")
uid = opf.get("unique-identifier")
if not any(e.get("id") == uid for e in opf.findall(".//dc:identifier", NS)):
    err(f"unique-identifier '{uid}' 没有对应的 dc:identifier 元素")

# 5) manifest href 全部存在 + media-type 非空
base = opf_path.rsplit("/", 1)[0] + "/"
items = opf.findall(".//o:manifest/o:item", NS)
ids = set()
for it in items:
    href, mt, iid = it.get("href"), it.get("media-type"), it.get("id")
    ids.add(iid)
    if not mt: err(f"manifest item {iid} 缺 media-type")
    if href.startswith("http"): continue
    if base + unquote(href) not in names:
        err(f"manifest href 不存在于包内: {href}")
if len(ids) != len(items): err("manifest 有重复 id")

# 6) nav 三件套
navitems = [it for it in items if "nav" in (it.get("properties") or "")]
if not navitems: err("manifest 缺 properties=nav 的项")
else:
    navd = etree.fromstring(z.read(base + navitems[0].get("href")))
    tocs = navd.findall(f".//{XHTML}nav")
    types = set()
    for t in tocs:
        ep = t.get("{http://www.idpf.org/2007/ops}type")
        types.add(ep)
        if ep == "toc":
            if t.find(f"{XHTML}ol") is None: err("nav[toc] 缺 ol")
    if "toc" not in types: err("nav 缺 epub:type='toc'")
    if "page-list" not in types: warn("nav 缺 page-list（非必需，但学术书应补）")
    # toc 中的 href 必须可达
    for a in navd.iter(f"{XHTML}a"):
        href = a.get("href")
        if not href or href.startswith("http"): continue
        f, _, anc = unquote(href).partition("#")
        if base + f not in names: err(f"nav 链接指向不存在的文件: {href}")
        elif anc:
            doc = etree.fromstring(z.read(base + f))
            if not doc.xpath(f"//*[@id='{anc}']"): err(f"nav 锚点不存在: {href}")

# 7) spine 引用
spine = opf.findall(".//o:spine/o:itemref", NS)
spine_ids = {e.get("idref") for e in spine}
if not spine_ids <= ids: err(f"spine 引用了 manifest 没有的 id: {spine_ids - ids}")
if not spine: err("spine 为空")

# 8) 每个 content 文档有 <title>
for it in items:
    if it.get("media-type") != "application/xhtml+xml": continue
    if (it.get("properties") or "") .find("nav") >= 0: continue
    d = etree.fromstring(z.read(base + unquote(it.get("href"))))
    if d.find(f".//{XHTML}title") is None:
        err(f"{it.get('href')} 缺 <title>")

print(f"=== {path.split('/')[-1]} ===")
print(f"ERRORS {len(errors)}  WARNINGS {len(warns)}")
for e in errors: print("  [E]", e)
for w in warns: print("  [W]", w)
sys.exit(1 if errors else 0)
