"""自由画布（网络草稿）的元素模型：**纯函数、零 IO、可复算**。

为什么不是"引文网络的另一种视图"：草稿是 AI 表达调查结果的地方，画什么都有可能——
一篇论文、一句结论、一个分组框、一条"这里还没人做"的注记。引文只是其中一种素材。
所以这里不建模"论文图"，只建模**元素 + 连线 + 框**：元素是什么由 ``kind`` 说，
坐标由布局引擎算（``infra/graphviz.py``），本模块只负责"把话翻译成 DOT"。

三条刻意的设计：

1. **``kind`` 不是白名单**。它只决定缺省样式；真正的形状由 ``shape`` 说（Graphviz 的
   shape 名全部开放），写了个没见过的 kind/shape 也照样能画出来——"形式自由"靠的是
   **不拦**，不是靠把可选项列全。白名单一加，人就得先问"支持哪些"，那就是规则。
2. **尺寸在 Python 侧算死**（``measure``）。页面是我们自己用 HTML 画节点（悬浮卡、跳详情页、
   拖拽缩放都得自己来），若让 Graphviz 按它的字体度量定尺寸，我们画出来的框和它留的
   位置对不上——文字会溢出、标签会压线。所以 ``fixedsize=true``，Graphviz 只负责**摆**。
3. **框（``parent``）与层位（``rank``）是元素属性，不是另一套坐标**。AI 说"这三篇归到
   量子网络这一框里""上游放一排"，这是语义；画成子图/同层是 Graphviz 的事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 元素种类 → 缺省形状。**只是缺省**：``shape`` 显式给了就用给的（Graphviz 任意 shape 名）。
#: 新增种类只改这里，不动校验、不动布局。
KIND_SHAPE: dict[str, str] = {
    "paper": "box",        # 论文：可点进详情页
    "text": "plaintext",   # 一句话／一段结论（无框，最轻）
    "note": "note",        # 便签：像手写贴纸，适合"待验证"这类注记
    "group": "box",        # 框：把别的元素包起来（走 subgraph cluster）
    "axis": "plaintext",   # 分区标题／泳道名
    "badge": "ellipse",    # 小圆点／标签
}
DEFAULT_KIND = "text"

#: 边的线型白名单（Graphviz style 里最常用的几个；给了别的也放行）
EDGE_STYLES = ("solid", "dashed", "dotted", "bold", "invis")
#: 箭头白名单（同 Graphviz arrowhead）
EDGE_ARROWS = ("normal", "none", "odot", "tee", "diamond", "vee", "empty", "inv")

#: 一行字按这个宽度折行（字符当量：CJK 算 2）
_LABEL_WRAP = 22
#: 字号（pt）与行高，供 measure 用；页面渲染同一套数字，两边必须一致
FONT_PT = 12
LINE_PT = 16
PAD_X_PT = 12
PAD_Y_PT = 8


@dataclass(frozen=True)
class CanvasNode:
    """一个元素。``id`` 是唯一句柄（连线按它引）。"""

    id: str
    kind: str = DEFAULT_KIND
    label: str = ""
    arxiv: str = ""                       # 有它 ⇒ 页面上的节点可点进 /papers/<id>
    parent: str = ""                      # 归属的 group 元素 id（画成框）
    shape: str = ""                       # 显式形状；空 ⇒ 按 kind 给
    width_pt: float = 0.0                 # 显式尺寸（pt）；0 ⇒ 由 label 量出来
    height_pt: float = 0.0
    fill: str = ""                        # 背景色；空 ⇒ 按 kind 的缺省
    color: str = ""                       # 边框色
    text_color: str = ""
    rank: int | None = None               # 显式层位（同值 ⇒ rank=same）
    meta: dict = field(default_factory=dict)

    @property
    def shape_name(self) -> str:
        return (self.shape or KIND_SHAPE.get(self.kind) or "box").strip()

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    @property
    def link(self) -> str:
        """站内跳转目标。只在有 arxiv 时给——**永不外跳**（外跳只发生在详情页）。"""
        return f"/papers/{self.arxiv}" if self.arxiv else ""


@dataclass(frozen=True)
class CanvasEdge:
    src: str
    dst: str
    label: str = ""
    style: str = "solid"
    color: str = ""
    arrow: str = "normal"
    constraint: bool = True                # False ⇒ 这条线不参与分层（跨层长线用）


def wrap_label(text: str, width: float = _LABEL_WRAP) -> list[str]:
    """把一句话折成若干行（CJK 算两格）。**页面与 DOT 共用**：量出来的宽度必须等于
    画出来的宽度，否则框和字对不上（长标签会溢出框）。"""
    out: list[str] = []
    for raw in (text or "").split("\n"):
        if not raw:
            out.append("")
            continue
        cur, cur_w = "", 0.0
        for ch in raw:
            cell = 2.0 if _is_wide(ch) else 1.0
            if cur_w + cell > width and cur:
                out.append(cur)
                cur, cur_w = "", 0.0
            cur += ch
            cur_w += cell
        out.append(cur)
    return out or [""]


def measure(label: str, width_pt: float = 0.0, height_pt: float = 0.0) -> tuple[float, float]:
    """按折行后的字数估尺寸（pt）。显式给了就用显式的。"""
    if width_pt > 0 and height_pt > 0:
        return width_pt, height_pt
    lines = wrap_label(label)
    widest = max(sum(2.0 if _is_wide(ch) else 1.0 for ch in line) for line in lines)
    w = width_pt or (widest * FONT_PT * 0.62 + PAD_X_PT * 2)
    h = height_pt or (len(lines) * LINE_PT + PAD_Y_PT * 2)
    return max(28.0, w), max(20.0, h)


def _is_wide(ch: str) -> bool:
    """CJK/全角按两个字符宽算（否则中文标签会被量窄，文字溢出框）。"""
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x20000 <= code <= 0x3FFFD
    )


# ---------------------------------------------------------------- 收敛（AI 给的形状千奇百怪，别崩）

_ID_BAD = re.compile(r"[^0-9A-Za-z_.:\-]+")


def norm_id(raw: Any, index: int) -> str:
    """DOT 的 id 不能随便写（引号/空格/箭头都是语法）。非法字符换掉，空则给个序号。"""
    text = _ID_BAD.sub("_", str(raw or "").strip())
    return text or f"e{index}"


def normalize(payload: dict | None) -> tuple[list[CanvasNode], list[CanvasEdge]]:
    """把任意来源的载荷收敛成元素表 + 连线表。

    容忍度是刻意的：AI 写 ``{"nodes": {"a": "标题"}}``、``{"nodes": [{"id": "a"}]}``、
    边写 ``"a->b"`` 或 ``["a", "b"]`` 都收；**不认识的字段一律忽略**（不是报错），
    因为"画什么都有可能"意味着我们没法预判字段，宁可少画一笔，也别让整张图交不上去。
    """
    raw = payload or {}
    nodes_raw = raw.get("nodes") or []
    if isinstance(nodes_raw, dict):
        nodes_raw = [{"id": k, "label": v if isinstance(v, str) else ""} for k, v in nodes_raw.items()]
    nodes: list[CanvasNode] = []
    seen: set[str] = set()
    for i, item in enumerate(nodes_raw):
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            continue
        nid = norm_id(item.get("id") or item.get("key"), i)
        if nid in seen:                            # 重名：图会静默少一个点，改名并留痕
            nid = f"{nid}#{i}"
        seen.add(nid)
        arxiv = str(item.get("arxiv") or item.get("arxiv_id") or "").strip()
        if not arxiv and re.fullmatch(r"\d{4}\.\d{4,5}", nid):
            arxiv = nid                             # id 本身就是编号 ⇒ 直接当论文
        # 没说 kind 时按"有没有 arxiv"推：论文节点 vs 文字节点（旧草稿全是论文）
        kind = str(item.get("kind") or item.get("type")
                   or ("paper" if arxiv else DEFAULT_KIND)).strip().lower()
        label = str(item.get("label") or item.get("text") or item.get("title") or "").strip()
        if not label and kind == "paper":
            label = arxiv or nid
        raw_rank = item.get("rank")
        try:
            rank = int(raw_rank) if raw_rank not in (None, "") else None
        except (TypeError, ValueError):
            rank = None
        nodes.append(CanvasNode(
            id=nid, kind=kind, label=label, arxiv=arxiv,
            parent=norm_id(item.get("parent"), -1) if item.get("parent") else "",
            shape=str(item.get("shape") or "").strip(),
            width_pt=_f(item.get("width") or item.get("w")),
            height_pt=_f(item.get("height") or item.get("h")),
            fill=str(item.get("fill") or item.get("background") or "").strip(),
            color=str(item.get("color") or item.get("border") or "").strip(),
            text_color=str(item.get("text_color") or "").strip(),
            rank=rank,
            meta={k: v for k, v in item.items() if k not in _NODE_FIELDS},
        ))
    kinds = {n.id: n for n in nodes}
    edges: list[CanvasEdge] = []
    for item in raw.get("edges") or raw.get("links") or []:
        src = dst = label = ""
        if isinstance(item, str):
            parts = re.split(r"\s*(?:->|→|=>)\s*", item, maxsplit=1)
            if len(parts) == 2:
                src, dst = parts
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            src, dst = item[0], item[1]
            label = str(item[2]) if len(item) > 2 else ""
        elif isinstance(item, dict):
            src = item.get("src") or item.get("from") or item.get("source") or ""
            dst = item.get("dst") or item.get("to") or item.get("target") or ""
            label = str(item.get("label") or "")
        src, dst = norm_id(src, -1), norm_id(dst, -2)
        if not src or not dst or src not in kinds or dst not in kinds:
            continue                                 # 悬空边：静默丢（不是整图报错）
        style = str((item or {}).get("style") or "solid") if isinstance(item, dict) else "solid"
        edges.append(CanvasEdge(
            src=src, dst=dst, label=label,
            style=style if style in EDGE_STYLES else "solid",
            color=str(item.get("color") or "").strip() if isinstance(item, dict) else "",
            arrow=(str(item.get("arrow") or "normal") if isinstance(item, dict) else "normal"),
            constraint=bool(item.get("constraint", True)) if isinstance(item, dict) else True,
        ))
    return nodes, edges


_NODE_FIELDS = {
    "id", "key", "kind", "type", "label", "text", "title", "arxiv", "arxiv_id", "parent",
    "shape", "width", "height", "w", "h", "fill", "background", "color", "border",
    "text_color", "rank",
}


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 翻译成 DOT

_GROUP_PALETTE = ("#eef2ff", "#f0fdf4", "#fef9c3", "#fdf2f8", "#ecfeff", "#f5f3ff")


def to_dot(nodes: list[CanvasNode], edges: list[CanvasEdge], *,
           rankdir: str = "TB", nodesep: float = 0.45, ranksep: float = 0.7,
           engine: str = "dot", label_edges: bool = True) -> str:
    """元素表 → DOT 文本。**只翻译，不判对错**（布局是引擎的事）。"""
    groups = [n for n in nodes if n.is_group]
    members: dict[str, list[CanvasNode]] = {"": [], **{g.id: [] for g in groups}}
    for n in nodes:
        if n.is_group:
            continue
        parent = n.parent if n.parent in members else ""
        members[parent].append(n)                 # parent 不在 ⇒ 落到顶层（不丢）

    out: list[str] = [
        "digraph canvas {",
        f'  graph [rankdir={rankdir}, nodesep={nodesep}, ranksep={ranksep}, '
        f'splines=true, pad=0.2, bgcolor="transparent"];',
        f'  node [shape=box, fontsize={FONT_PT}, penwidth=1.2, margin=0.04];',
        '  edge [fontsize=10, arrowsize=0.7, penwidth=1.1, color="#94a3b8"];',
        '  // 跨层长边允许绕行，别把分层挤歪',
        '  graph [ordering=out];',
    ]

    def node_line(n: CanvasNode, stem: str) -> str:
        w_pt, h_pt = measure(n.label, n.width_pt, n.height_pt)
        attrs = {
            "label": n.label,
            "shape": n.shape_name,
            "width": f"{w_pt / 72.0:.4f}",
            "height": f"{h_pt / 72.0:.4f}",
            "fixedsize": "true",
        }
        fill = fill_of(n)
        if fill:
            attrs["fillcolor"] = fill
            attrs["style"] = "filled" if n.shape_name not in ("plaintext", "none") else "filled"
        if n.color:
            attrs["color"] = n.color
        if n.text_color:
            attrs["fontcolor"] = n.text_color
        if n.shape_name in ("plaintext", "none"):
            attrs.pop("style", None)
            attrs.pop("fillcolor", None)
        return f"  {stem} [{_attrs(attrs)}];"

    # 顶层元素
    for n in members[""]:
        out.append(node_line(n, n.id))
    # 分组框（subgraph cluster_ 前缀是 Graphviz 约定，不是我们的命名）
    for gi, g in enumerate(groups):
        out.append(f"  subgraph cluster_{gi} {{")
        out.append(f'    label={_q(g.label or "")}; fontsize={FONT_PT + 1}; '
                   f'color={_q(g.color or "#cbd5e1")}; style=rounded; penwidth=1.1;')
        if g.fill:
            out.append(f"    bgcolor={_q(g.fill)};")
        for n in members[g.id]:
            out.append(node_line(n, n.id))
        if not members[g.id]:
            out.append(f'    "{g.id}_empty" [shape=point, style=invis];')
        out.append("  }")
    # 同层约束：rank 相同的并排
    by_rank: dict[int, list[str]] = {}
    for n in nodes:
        if n.rank is not None and not n.is_group:   # 框不是点，进 rank=same 会被画成一个真节点
            by_rank.setdefault(int(n.rank), []).append(n.id)
    for _rank, ids in sorted(by_rank.items()):
        if len(ids) > 1:
            out.append(f"  {{ rank=same; {'; '.join(ids)}; }}")
    for e in edges:
        attrs = {"label": e.label if label_edges else "", "style": e.style,
                 "arrowhead": e.arrow, "constraint": "true" if e.constraint else "false"}
        if e.color:
            attrs["color"] = e.color
        if not e.label:
            attrs.pop("label")
        out.append(f"  {e.src} -> {e.dst} [{_attrs(attrs)}];")
    out.append("}")
    return "\n".join(out)


def fill_of(n: CanvasNode) -> str:
    """元素的底色：显式给了就用给的，否则按 kind 的缺省。**页面与 DOT 共用这一份**，
    否则框的留白和页面画的块对不上。"""
    if n.fill:
        return n.fill
    return {
        "paper": "#e0e7ff",
        "note": "#fef3c7",
        "badge": "#f1f5f9",
        "group": "#f8fafc",
        "axis": "",
        "text": "",
    }.get(n.kind, "")


def _q(text: str) -> str:
    """DOT 字符串转义。**真换行必须换成两个字符**——DOT 的引号串里不允许裸换行
    （实测踩过：标签里带换行的便签直接让 dot 报 syntax error in line 8）。"""
    safe = (str(text).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n"))
    return f'"{safe}"'


def _attrs(attrs: dict[str, Any]) -> str:
    """属性表 → ``k=v`` 串。**值一律加引号**：``fillcolor=#eef2ff`` 这种裸值里的 ``#``
    会被 DOT 当成注释起始（实测：整行语法错）。加引号对数字/布尔同样合法。"""
    parts = []
    for key, val in attrs.items():
        if val is None or val == "":
            continue
        parts.append(f"{key}={_q(val)}")
    return ", ".join(parts)
