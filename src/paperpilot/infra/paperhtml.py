"""arXiv **HTML 正文**：抓取 → 消毒 → 离线化 → 解析成可锚定的块。

为什么不走 PDF（`SPEC.md` N2 原写的是 PDF→文本）：PDF 是二进制、没有结构、没有稳定 id，
既没法在界面里渲染，也没法把批注钉在"这句话"上。arXiv 自 2023-12 起为多数投稿提供
**LaTeXML 生成的 HTML**：章节/段落/公式/图表都带稳定 id（`S3.p2`、`S3.F1`、`S3.E1`），
这正好是精读与批注需要的锚点基底。⇒ **没有 HTML 的论文不进精读体系**（明说的取舍）。

本模块只做"与外部世界打交道 + 纯函数式解析"，不碰库、不碰 HTTP 路由：

- :func:`fetch_html` —— 按官方 → ar5iv 的顺序取正文；取不到就如实报 ``no_html``。
- :func:`retrieve_assets` —— **全量离线**：把 CSS/图片抓下来、把引用改写成站内路径。
- :func:`sanitize` —— 剥脚本/事件属性/``javascript:``（外部内容，必须当敌意内容处理）。
- :func:`extract_blocks` —— 展平成"块"清单：每块有 id、类型、所属章节、**纯文本**。
- :func:`locate_quote` —— **把一句话变成精确字符区间**（AI 只给 quote，几何由这里算）。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html as LH

#: 正文来源（按顺序试）。官方 html 是首选；ar5iv 是社区对老论文的 LaTeXML 渲染。
SOURCE_TEMPLATES = (
    ("arxiv", "https://arxiv.org/html/{aid}v{v}"),
    ("arxiv", "https://arxiv.org/html/{aid}"),
    ("ar5iv", "https://ar5iv.labs.arxiv.org/html/{aid}"),
)

#: 抓取礼貌：正文页面 3s（与 arXiv API 同款），**资源文件 0.5s**。
#: 为什么分开：一篇的 CSS/图片常有几十个（实测上限 200），全按 3s 会让"抓一篇"变成十分钟。
DOC_INTERVAL_SEC = 3.0
ASSET_INTERVAL_SEC = 0.5

#: 单个文件与单篇总资产的体积上限（防止一篇把磁盘吃光）。
MAX_ASSET_BYTES = 12 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 60 * 1024 * 1024
MAX_ASSETS = 200

#: 段落级块（可被批注的最小单位之外，还能整块引用）
_BLOCK_TAGS = {"p", "li", "figcaption", "td", "th", "blockquote"}
#: 整块引用型（图表、公式、章节）
_ATOMIC_TAGS = {"figure", "table", "section"}
#: 这些标签不打成"块"（它们是容器/噪声）
_SKIP_TAGS = {"script", "style", "noscript", "head", "nav", "footer"}


@dataclass
class Block:
    """一个可锚定的块。``text`` 与浏览器里的 ``textContent`` 对齐（同一套拼接规则）。"""

    block_id: str
    kind: str                  # paragraph | section | figure | table | equation | list | other
    section: str = ""          # 人类可读的章节路径（如 "3 Method" ）
    text: str = ""
    depth: int = 0

    def short(self, n: int = 60) -> str:
        t = " ".join(self.text.split())
        return t[:n] + ("…" if len(t) > n else "")


@dataclass
class FetchResult:
    ok: bool
    source: str = ""
    url: str = ""
    html: str = ""
    reason: str = ""           # no_html | http_error | network_error
    detail: str = ""


@dataclass
class Anchor:
    """批注的锚点：**块 id + 字符区间**为主，quote 前后文为兜底（换版本也能重找）。"""

    block: str
    start: int
    end: int
    quote: str = ""
    prefix: str = ""
    suffix: str = ""
    kind: str = "paragraph"

    def as_dict(self) -> dict:
        return {"block": self.block, "start": self.start, "end": self.end,
                "quote": self.quote, "prefix": self.prefix, "suffix": self.suffix,
                "kind": self.kind}


@dataclass
class LocateResult:
    anchors: list[Anchor] = field(default_factory=list)
    candidates: int = 0        # 命中了几处（>1 ⇒ 有歧义，要 AI 选）
    reason: str = ""           # ok | not_found | ambiguous | block_not_found


# ---------------------------------------------------------------- 抓取


class PaperHtmlClient:
    """极小的抓取器：只负责"取字节"，礼貌间隔与 UA 在这里统一。"""

    def __init__(self, *, timeout: float = 30.0, user_agent: str | None = None,
                 interval: float = ASSET_INTERVAL_SEC):
        self._last = 0.0
        self.interval = float(interval)
        self._client = httpx.Client(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": user_agent or
                     "PaperPilot/0.1 (personal literature assistant; local use)"})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PaperHtmlClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _wait(self, interval: float | None = None) -> None:
        gap = time.monotonic() - self._last
        need = self.interval if interval is None else float(interval)
        if gap < need:
            time.sleep(need - gap)
        self._last = time.monotonic()

    def get(self, url: str, *, interval: float | None = None) -> httpx.Response | None:
        self._wait(interval)
        try:
            return self._client.get(url)
        except httpx.HTTPError:
            return None


def fetch_html(client: PaperHtmlClient, arxiv_id: str, version: int | None = None) -> FetchResult:
    """取一篇论文的 HTML 正文。取不到**如实报** ``no_html``（不猜、不回落 PDF）。"""
    for source, tpl in SOURCE_TEMPLATES:
        if "{v}" in tpl and not version:
            continue
        url = tpl.format(aid=arxiv_id, v=version or "")
        resp = client.get(url, interval=DOC_INTERVAL_SEC)
        if resp is None:
            continue
        if resp.status_code == 404:
            continue
        if resp.status_code >= 400:
            return FetchResult(False, source, url, "", "http_error", f"HTTP {resp.status_code}")
        text = resp.text or ""
        if len(text) < 2000 or "<html" not in text[:4000].lower():
            continue                      # 拿到了但不是正文页（如占位/跳转页）
        return FetchResult(True, source, url, text)
    return FetchResult(False, "", "", "", "no_html",
                       "arXiv 没有为这篇产出 HTML（老论文且 ar5iv 也没有）⇒ 不进精读体系")


# ---------------------------------------------------------------- 消毒 + 离线


def sanitize(root) -> dict:
    """剥掉一切"会执行"的东西。**外部内容按敌意内容处理**。

    不改 id、不改文本、不动结构——锚点语义全靠它们，动了批注就全错位。
    """
    killed = {"script": 0, "style_attr": 0, "handlers": 0, "js_url": 0, "base": 0}
    for el in root.iter():
        tag = (el.tag if isinstance(el.tag, str) else "").lower()
        if tag in ("script", "noscript", "iframe", "object", "embed"):
            el.getparent().remove(el) if el.getparent() is not None else None
            killed["script"] += 1
            continue
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            low = attr.lower()
            if low.startswith("on"):
                del el.attrib[attr]
                killed["handlers"] += 1
            elif low in ("href", "src", "action", "formaction", "xlink:href"):
                if el.attrib[attr].strip().lower().startswith("javascript:"):
                    del el.attrib[attr]
                    killed["js_url"] += 1
        if tag == "base":
            el.getparent().remove(el) if el.getparent() is not None else None
            killed["base"] += 1
    return killed


def _asset_dest(name: str, url: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(urlparse(url).path).name) or "asset"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"assets/{digest}-{stem[:60]}" or name


def retrieve_assets(client: PaperHtmlClient, root, base_url: str, dest: Path,
                    *, app_prefix: str) -> dict:
    """**全量离线**：把 CSS/图片抓下来、把引用改写成站内路径 ``{app_prefix}/assets/…``。

    只认 ``link[rel=stylesheet]``、``img[src]``、``source[src]``、``img[srcset]`` 的第一项
    与 CSS 里的 ``url(...)``（一层）。抓不到就**保留绝对外链**——离线不完整也不毁页面。

    ``app_prefix`` 形如 ``/paper/2508.06639``，最终引用写成 ``/paper/2508.06639/assets/xx.css``。
    """
    dest.mkdir(parents=True, exist_ok=True)
    cache: dict[str, str] = {}
    total = 0
    count = 0
    failed: list[str] = []

    def localize(raw: str) -> str:
        nonlocal total, count
        if not raw or raw.startswith(("data:", "#", "mailto:")):
            return raw
        url = urljoin(base_url, raw.strip())
        if not url.startswith(("http://", "https://")):
            return raw
        if url in cache:
            return cache[url]
        if count >= MAX_ASSETS or total >= MAX_TOTAL_ASSET_BYTES:
            failed.append(url)
            return url
        resp = client.get(url)
        if resp is None or resp.status_code >= 400:
            failed.append(url)
            return url
        blob = resp.content or b""
        if not blob or len(blob) > MAX_ASSET_BYTES:
            failed.append(url)
            return url
        rel = _asset_dest("", url)
        out = dest / Path(rel).name
        out.write_bytes(blob)
        total += len(blob)
        count += 1
        served = f"{app_prefix}/{rel}"
        cache[url] = served
        if url.lower().endswith(".css"):                     # CSS 里的 url(...) 也离线
            text = blob.decode("utf-8", "replace")
            def _css(m: re.Match) -> str:
                inner = m.group(1).strip("'\"")
                return f"url({localize(urljoin(url, inner))})"
            rewritten = re.sub(r"url\(([^)]+)\)", _css, text)
            if rewritten != text:
                out.write_text(rewritten, encoding="utf-8")
        return served

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        if tag == "link" and "stylesheet" in (el.get("rel") or "").lower():
            href = el.get("href")
            if href:
                el.set("href", localize(href))
        elif tag in ("img", "source", "audio", "video"):
            src = el.get("src")
            if src:
                el.set("src", localize(src))
            srcset = el.get("srcset")
            if srcset:
                first = srcset.split(",")[0].strip().split(" ")[0]
                el.set("srcset", localize(first))
        elif tag == "a":
            href = el.get("href")
            if href and not href.startswith(("#", "mailto:", "javascript:")):
                el.set("href", urljoin(base_url, href))       # 外链一律绝对化
    return {"assets": count, "bytes": total, "failed": failed}


#: 应当可被锚定的元素（缺 id 时由 `ensure_block_ids` 补确定性 id）。
#: 函数本体在下面「解析成块」一节（要跟 `content_root`/`_skipped_subtree` 一起读）。
_BLOCK_ID_TAGS = {"p", "li", "figcaption", "td", "th", "blockquote", "figure", "table"}


def localize_and_clean(html_text: str, *, base_url: str, client: PaperHtmlClient,
                       dest: Path, app_prefix: str) -> tuple[str, dict]:
    """消毒 + 补 id + 离线化，返回 ``(改写后的 HTML, 统计)``。"""
    root = LH.fromstring(html_text)
    killed = sanitize(root)
    killed["ids_added"] = ensure_block_ids(root)
    moved = retrieve_assets(client, root, base_url, dest, app_prefix=app_prefix)
    out = LH.tostring(root, encoding="unicode", method="html")
    return out, {**killed, **moved}


# ---------------------------------------------------------------- 解析成块
#
# ⚠ 下面这套规则是**对着真实 arXiv HTML5 输出**定出来的（不是照文档猜的）。实测骨架：
#   body > dialog#modal-form / div#announcement-banner / header / nav.ltx_page_navbar /
#          **div.ltx_page_main** > article.ltx_document > (h1 标题 / div#abstract1.ltx_abstract
#          / div#p1.ltx_para > p#p1.1.ltx_p / figure#S0.F1 / …) / footer ×2 / div#fixed-buttons
#   ⇒ 三件事必须做对：**① 先缩到正文容器**（否则导航/弹窗/页脚都成了"块"）；
#     ② 段落认 `div.ltx_para` 里的 `<p>`（id 形如 p1.1）；③ 参考文献整棵子树别铺开
#     （一篇 78 条文献会把 166 个块灌进来，读通道被它挤爆）。

#: 正文容器类名（按优先级）：arXiv 页面把非正文塞在同一个 body 里。
_MAIN_CLASSES = ("ltx_page_main", "ltx_document")
#: 整棵子树都不做成块的类名（参考文献、导航、术语表这类）。
_SUBTREE_SKIP_CLASSES = ("ltx_bibliography", "ltx_page_navbar", "keyboard-glossary")
#: 这些标签不打成"块"（容器/噪声）
_SKIP_TAGS = {"script", "style", "noscript", "head", "nav", "header", "footer", "dialog"}


def _classes(el) -> list[str]:
    return (el.get("class") or "").split() if isinstance(el.tag, str) else []


def _find_by_class(scope, name: str):
    for el in scope.iter():
        if isinstance(el.tag, str) and name in _classes(el):
            return el
    return None


def _skipped_subtree(el) -> bool:
    cur = el
    while cur is not None:
        if any(c in _SUBTREE_SKIP_CLASSES for c in _classes(cur)):
            return True
        cur = cur.getparent()
    return False


def content_root(root):
    """正文真正的容器。**必须先用它**——否则 arXiv 的导航/弹窗/页脚会变成"正文块"。"""
    main = _find_by_class(root, _MAIN_CLASSES[0])
    if main is None:
        main = root.find("body")
    if main is None:
        main = root
    art = _find_by_class(main, _MAIN_CLASSES[1])
    return art if art is not None else main


def _kind_of(el) -> str:
    tag = (el.tag or "").lower()
    parts = _classes(el)
    joined = " ".join(parts)
    if "ltx_bibliography" in parts or el.get("id") == "bib":
        return "bibliography"
    if "ltx_abstract" in parts:
        # ⚠ **摘要是"标记"不是"容器节"**：若把它当普通 section，它会把后面整篇正文都吸成
        # 自己的子块（实测 2508.06639：128 块全挂在 Abstract 下，大纲彻底失真）。
        return "abstract"
    if tag == "section" or any(p.startswith(("ltx_section", "ltx_subsection", "ltx_paragraph"))
                               for p in parts):
        return "section"
    if tag == "figure" or "ltx_figure" in parts:
        return "figure"
    if tag == "table" or "ltx_table" in parts:
        return "table"
    if tag == "math" or any(p.startswith("ltx_eqn") for p in parts) or "equation" in joined:
        return "equation"
    if tag in ("ul", "ol"):
        return "list"
    if tag in _BLOCK_TAGS or "ltx_p" in parts:
        return "paragraph"
    return "other"


def _section_title(el) -> str:
    """节标题：认 ``h1``-``h6``，也认带 ``ltx_title`` 类的元素（新版 LaTeXML 两种都用）。"""
    for h in el.iter():
        if not isinstance(h.tag, str):
            continue
        tag = h.tag.lower()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") or "ltx_title" in _classes(h):
            t = " ".join((h.text_content() or "").split())
            if t and len(t) <= 140:
                return t
    return ""


def ensure_block_ids(root) -> int:
    """给缺失 ``id`` 的可锚定元素补一个**确定性** id（``pp-1``、``pp-2``…按文档序）。

    为什么必须有：LaTeXML 通常给段落 ``p1.1`` 这样的 id，但**不保证每个都有**——没有 id 的
    段落既进不了块清单、也没法锚定，会被整段丢掉。补 id 的时机是**抓取落盘那一刻**
    ⇒ 存档的 HTML 与批注锚点从此自洽（换版本时靠 quote/prefix/suffix 重锚）。
    参考文献整棵子树跳过：那里面补出来的 id 没人会去锚，只会污染块清单。
    """
    n = 0
    scope = content_root(root)
    for el in scope.iter():
        if not isinstance(el.tag, str) or (el.tag or "").lower() not in _BLOCK_ID_TAGS:
            continue
        if el.get("id") or _skipped_subtree(el):
            continue
        n += 1
        el.set("id", f"pp-{n}")
    return n


def extract_blocks(root) -> list[Block]:
    """把正文展平成块清单——**读通道与锚定都吃这份清单**。

    规则（与浏览器的 ``textContent`` 对齐，否则字符区间会错位）：
    - 先缩到 :func:`content_root`（正文容器），再跳过 :data:`_SUBTREE_SKIP_CLASSES` 子树；
    - 有 ``id`` 的段落/图表/公式/章节各成一块；纯文本 = ``text_content()``；
    - 收录后**不再往里钻**（避免父子重复计长 ⇒ 字符区间错位）。
    """
    blocks: list[Block] = []
    scope = content_root(root)

    def walk(el, depth: int, section: str) -> None:
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if (child.tag or "").lower() in _SKIP_TAGS or _skipped_subtree(child):
                continue
            kind = _kind_of(child)
            sec = section
            if kind == "section":
                title = _section_title(child) or (child.get("id") or "（无标题节）")
                sec = title
                if child.get("id"):
                    blocks.append(Block(child.get("id"), "section", title, title, depth))
            elif kind == "abstract":
                # 摘要：**本身只作一个标记块**（标题），内层 <p id=abstract1.1> 才是正文——
                # 不这么分，摘要容器的 text_content 会和内层段落**重复计长**（字符区间就错位了）。
                title = _section_title(child) or "Abstract"
                if child.get("id"):
                    blocks.append(Block(child.get("id"), "abstract", title, title, depth))
                walk(child, depth + 1, title)
                continue
            elif kind == "bibliography":
                if child.get("id"):
                    blocks.append(Block(child.get("id"), "bibliography", sec,
                                        _section_title(child) or "References", depth))
                    continue                       # 整棵子树不再展开（78 条文献不是"正文块"）
            elif kind != "other" and child.get("id"):
                text = child.text_content() or ""
                if text.strip():
                    blocks.append(Block(child.get("id"), kind, sec, text, depth))
                    continue                       # 已收录 ⇒ 不再往里钻（防重复计长）
            walk(child, depth + 1, sec)

    walk(scope, 0, "")
    return blocks


def outline(blocks: list[Block]) -> list[dict]:
    """章节树 + 每节的块数与类型分布（给 AI 一张"目录 + 锚点地图"）。

    ⚠ **按块自己的 ``section`` 字段归属**（那是遍历时就确定的），不按"位置在谁后面"推断——
    后者会让"摘要"把后面整篇正文都吸成自己的子块（实测 2508.06639 曾出现 128 块挂在
    Abstract 下），也会在**无分节论文**上彻底失真（Nature 体例的信件就是那样，实测只有
    References）。无节可归的块统一落到 ``（无分节）``。
    """
    order: list[str] = []
    agg: dict[str, dict] = {}

    def slot(key: str, ident: str = "") -> dict:
        if key not in agg:
            agg[key] = {"section": key, "id": ident, "blocks": 0, "kinds": {}}
            order.append(key)
        if ident and not agg[key]["id"]:
            agg[key]["id"] = ident
        return agg[key]

    for b in blocks:
        if b.kind in ("section", "abstract", "bibliography"):
            slot(b.text or b.block_id, b.block_id)
            continue
        entry = slot(b.section or "（无分节）")
        entry["blocks"] += 1
        entry["kinds"][b.kind] = entry["kinds"].get(b.kind, 0) + 1
    return [agg[k] for k in order]


# ---------------------------------------------------------------- 锚定


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def locate_quote(blocks: list[Block], quote: str, *, block_id: str = "",
                 window: int = 40) -> LocateResult:
    """**把一句话变成精确字符区间**——AI 只给"标哪句话"，几何由这里算。

    解析顺序（越靠前越可信）：
    1. 指定了 ``block_id`` ⇒ 只在该块里找；
    2. 全文精确匹配（**唯一**才算命中，多处 ⇒ 返回候选让 AI 指定块）；
    3. 归一化空白后再试（论文 HTML 里的换行/多空格很常见）；
    4. 都不中 ⇒ ``not_found``，提示换一句更独特的原文。
    """
    q = (quote or "").strip()
    if not q:
        return LocateResult(reason="not_found")
    pools = blocks
    if block_id:
        pools = [b for b in blocks if b.block_id == block_id]
        if not pools:
            return LocateResult(reason="block_not_found")

    def hits(text: str, needle: str) -> list[int]:
        out, i = [], text.find(needle)
        while i >= 0:
            out.append(i)
            i = text.find(needle, i + 1)
        return out

    for needle in (q, _norm(q)):
        found: list[Anchor] = []
        for b in pools:
            hay = b.text if needle == q else _norm(b.text)
            for i in hits(hay, needle):
                if needle == q:
                    start, end = i, i + len(needle)
                    pref, suff = b.text[max(0, i - window):i], b.text[end:end + window]
                else:                                     # 归一化命中：偏移只在归一化串上可靠
                    start, end = 0, len(b.text)
                    pref, suff = "", ""
                found.append(Anchor(block=b.block_id, start=start, end=end,
                                    quote=q, prefix=pref, suffix=suff, kind=b.kind))
        if len(found) == 1:
            return LocateResult(anchors=found, candidates=1, reason="ok")
        if len(found) > 1:
            if block_id:
                return LocateResult(anchors=found[:1], candidates=len(found), reason="ok")
            return LocateResult(anchors=found[:5], candidates=len(found), reason="ambiguous")
    return LocateResult(reason="not_found")


def relocate(blocks: list[Block], anchor: dict) -> LocateResult:
    """**重锚**：拿旧锚点回新正文里重找（换版本/重抓之后用）。先按 quote，再按块+区间核验。"""
    block_id = str(anchor.get("block") or "")
    quote = str(anchor.get("quote") or "")
    for b in blocks:
        if b.block_id != block_id:
            continue
        start, end = int(anchor.get("start") or 0), int(anchor.get("end") or 0)
        if quote and b.text[start:end] == quote:
            return LocateResult(anchors=[Anchor(block_id, start, end, quote,
                                                kind=b.kind)], candidates=1, reason="ok")
    if quote:
        return locate_quote(blocks, quote)
    return LocateResult(reason="not_found")


def snippet(text: str, start: int, end: int, *, pad: int = 80) -> str:
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    head = "…" if a > 0 else ""
    tail = "…" if b < len(text) else ""
    return f"{head}{text[a:b]}{tail}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
