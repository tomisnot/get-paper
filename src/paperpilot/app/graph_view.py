"""图视图装配（消费方适配器）：**一份 ViewSpec → 一份渲染载荷**。

为什么单开一个模块：此前 /network 路由里散着一堆 if（颜色、标签、截断、边采样各写各的），
于是"AI 画的图"和"页面显示的图"是两个真相——改一处忘一处，箭头被节点盖住这类问题
没人能在交付前看见。现在：视图（根/深度/布局/分组/着色/标签/预算/锚点）是**数据**，
本模块把它编译成载荷，三个消费者共用：

1. ``/network``（HTML 渲染）；
2. ``/network.json``（**渲染回执**：AI 自查几何/配色/裁切，不必等人截图）；
3. ``set_graph_view``（发布视图，落 ``graph_views`` 表）。

域层（``domain/graph.py``）只管几何；本层才允许出现领域词汇（标题/标签/被引数…）。
"""

from __future__ import annotations

import math

from ..domain.graph import (
    Edge as GEdge,
)
from ..domain.graph import (
    Node as GNode,
)
from ..domain.graph import label_for, layered_layout, short_id, timeline_layout
from ..domain.profile import paper_features, score_paper

#: 六色图论标签（AI 用 tag_paper 钉，图例/着色/角标都读它）
TAG_COLORS: dict[str, str] = {
    "平台源头": "#2563eb",
    "理论源头": "#7c3aed",
    "综述枢纽": "#059669",
    "实验谱系": "#d97706",
    "下游扩散": "#dc2626",
    "动机": "#0891b2",
}

#: 泳道缺省顺序＝**因果序**（动机 → 源头 → 综合 → 实验 → 扩散）：与 TAG_COLORS 的字典序
#: 无关，也**不依赖调用方传 group_order**。为什么写死在产品侧：实测发现"未声明的参数会被
#: 工具调用链静默丢掉"，而"泳道按什么顺序排"是产品语义，不该指望每次调用都传对。
TAG_ORDER: tuple[str, ...] = ("动机", "平台源头", "理论源头", "综述枢纽", "实验谱系", "下游扩散")

#: 自定义分组的调色板（视图可自带 group_colors 覆盖）
GROUP_PALETTE: tuple[str, ...] = (
    "#2563eb", "#7c3aed", "#059669", "#d97706", "#dc2626", "#0891b2",
    "#db2777", "#65a30d", "#0284c7", "#9333ea",
)

#: 视图规范缺省值（任何来源的 spec 都先与它合并，再做白名单收敛）
DEFAULT_SPEC: dict = {
    "root": "",              # 单根聚焦；空＝全库视角
    "depth": 2,              # 有向半径（上游/下游各几跳）
    "sides": "both",         # both（根居中，上游在上/下游在下）|upstream|downstream
    "layout": "layer",       # layer|timeline
    "color_by": "auto",      # auto|kind|in_lib|weight|tag|group
    "group_by": "none",      # none|tag|group
    "label_mode": "auto",    # auto|always|hover
    "label_style": "title",  # title|id
    "label_max": 18,
    "label_auto_max": 90,    # auto 模式下"布点数 ≤ 此值就常显标签"（默认视图 75 点即可见）
    "badge": True,           # 标签角标（文字，不只靠颜色）
    "in_lib_only": False,    # 只在图上放库内论文（缺的用 materialize_view 入库，别靠隐藏）
    "arrow_size": 13,
    "max_nodes": 90,
    "max_edges": 400,
    "group_quota": 0,        # 每组保底篇数（0=不保底）
    "sort_within": "weight",  # weight|year|align（align=按连接重心对齐，连线最短）
    "size_by": "degree",     # degree|weight|flat
    "layer_gap": 130,
    "node_gap": 90,
    "pin": [],               # 锚点：永不截断
    "rank": [],              # **AI 的显式优先级**（id 顺序）：压过度数/分组 —— 我认定的骨干
    "place": "lane",         # lane（按组聚簇成泳道）| center（按 rank 从行中心向两侧展开）
    "layers": {},            # **AI 的自定义分层**：id → 有向层号（−2＝上游两跳、0＝本体、+1＝下游）
    "mode": "auto",          # auto＝机器替我捞一圈的**草稿** | curated＝**我点名的清单**（layers 即内容）
    "group_map": {},         # arxiv → 组名（AI 自定义叙事分组）
    "group_colors": {},      # 组名 → 色值
    "group_order": [],       # 组的显示顺序
    "title": "",             # 视图标题（页头一行话）
}

_INT_RANGES = {
    "depth": (1, 6), "max_nodes": (1, 400), "max_edges": (1, 3000),
    "arrow_size": (6, 40), "group_quota": (0, 50), "label_max": (6, 60),
    "label_auto_max": (0, 400), "layer_gap": (40, 400), "node_gap": (24, 300),
}
_ENUMS = {
    "layout": ("layer", "timeline"),
    "sides": ("both", "upstream", "downstream"),
    "place": ("lane", "center"),
    "mode": ("auto", "curated"),
    "color_by": ("auto", "kind", "in_lib", "weight", "tag", "group"),
    "group_by": ("none", "tag", "group"),
    "label_mode": ("auto", "always", "hover"),
    "label_style": ("title", "id"),
    "sort_within": ("weight", "year", "align"),
    "size_by": ("degree", "weight", "flat"),
}


def normalize_spec(raw: dict | None) -> dict:
    """把任意来源（视图行 / query 参数 / 缺省）收敛成规范 ViewSpec（白名单+类型+值域）。"""
    spec = dict(DEFAULT_SPEC)
    for k, v in (raw or {}).items():
        if k in spec and v is not None and v != "":
            spec[k] = v
    for key, (lo, hi) in _INT_RANGES.items():
        try:
            spec[key] = max(lo, min(hi, int(spec[key])))
        except (TypeError, ValueError):
            spec[key] = DEFAULT_SPEC[key]
    for key, allowed in _ENUMS.items():
        if spec[key] not in allowed:
            spec[key] = DEFAULT_SPEC[key]
    spec["badge"] = bool(spec["badge"]) if not isinstance(spec["badge"], str) \
        else spec["badge"].lower() not in ("0", "false", "no", "off")
    spec["in_lib_only"] = bool(spec["in_lib_only"]) if not isinstance(spec["in_lib_only"], str) \
        else spec["in_lib_only"].lower() not in ("0", "false", "no", "off")
    spec["root"] = str(spec["root"] or "").strip()
    spec["title"] = str(spec["title"] or "").strip()
    for key in ("pin", "rank", "group_order"):
        val = spec[key]
        spec[key] = [str(x).strip() for x in val if str(x).strip()] if isinstance(val, (list, tuple)) \
            else [x.strip() for x in str(val).split(",") if x.strip()]
    for key in ("group_map", "group_colors"):
        spec[key] = dict(spec[key]) if isinstance(spec[key], dict) else {}
    # 自定义分层：'id:-2,id2:1'（有向层号：负=上游、0=本体、正=下游）。非法项丢弃不崩。
    if not isinstance(spec["layers"], dict):
        parsed: dict[str, int] = {}
        for chunk in str(spec["layers"]).replace(";", ",").split(","):
            ident, _, val = chunk.partition(":")
            ident, val = ident.strip(), val.strip()
            if not ident or not val:
                continue
            try:
                parsed[ident] = max(-6, min(6, int(val)))
            except ValueError:
                continue
        spec["layers"] = parsed
    else:
        spec["layers"] = {str(k): max(-6, min(6, int(v)))
                          for k, v in spec["layers"].items() if str(k).strip()}
    return spec


def spec_from_query(query: dict, base: dict) -> dict:
    """query 参数覆盖视图 spec（显式意图 > 视图 > 配置）：只认白名单键。"""
    raw = dict(base)
    for key in DEFAULT_SPEC:
        if key in query and str(query[key]) != "":
            raw[key] = query[key]
    if "pin" in query and str(query["pin"]):
        raw["pin"] = str(query["pin"]).split(",")
    return normalize_spec(raw)


def _resolve_color(by: str, node_id: str, *, tag: str, group: str, in_lib: bool,
                   weight: float, group_colors: dict) -> str:
    if by == "tag":
        return TAG_COLORS.get(tag, "#94a3b8")
    if by == "group":
        if group and group in group_colors:
            return group_colors[group]
        keys = [g for g in group_colors] or []
        idx = keys.index(group) % len(GROUP_PALETTE) if group in keys else 0
        return GROUP_PALETTE[idx] if group else "#94a3b8"
    if by == "in_lib":
        return "#4f46e5" if in_lib else "#64748b"
    if by == "weight":
        return "#4f46e5" if in_lib else "#94a3b8"
    return "#4f46e5" if in_lib else "#64748b"          # kind


def _card(retrieval, node_id: str, meta: dict) -> dict:
    """悬浮卡：**图不生产正文**——文字从卡片系统（summary）与消费方 meta 拉。"""
    facts: list[str] = []
    if meta.get("in_lib"):
        if meta.get("sc") is not None:
            facts.append(f"画像分 {meta.get('sc', 0.0):.2f}")
        if meta.get("citations"):
            facts.append(f"S2 被引 {meta['citations']}")
        detail = retrieval.detail(node_id) if retrieval is not None else None
        s = detail["summary"] if detail else None
        lines = []
        if s:
            for tag, val in (("TL;DR", s.tldr), ("问题", s.problem), ("方法", s.method),
                             ("结论", s.results), ("贡献", s.novelty)):
                if val:
                    lines.append(f"{tag}：{val}")
        else:
            lines = ["还没卡：在 dsh 让 AI write_summary 补一张"]
    else:
        facts += [f"库内同引 {meta.get('cites', 0)} 篇", f"S2 被引 {meta.get('citations', 0)}"]
        lines = ["未入库：点击＝拉进入库并进管理页，卡由 AI 按需补"]
    if meta.get("in_lib") and meta.get("cites"):
        facts.append(f"库内同引 {meta['cites']} 篇")
    return {"title": meta.get("title") or node_id, "facts": facts, "lines": lines}


def _signed_depths(edges, root: str, depth: int, sides: str) -> dict[str, int]:
    """**有向分层**：0＝本体；负＝上游（它引用的，沿"引用"边回溯）；正＝下游（引用它的）。

    这是"上下层该表现祖先/后代"的核心：无向 BFS 会把"它引的"和"引用它的"混在同一层，
    看不出方向。上游只沿出边回溯、下游只沿入边外扩（互不串门），到 ``depth`` 跳为止；
    ``sides`` 决定只留哪一侧（both/upstream/downstream）。
    """
    outs: dict[str, set[str]] = {}      # X → X 引用的（上游候选）
    ins: dict[str, set[str]] = {}       # X → 引用了 X 的（下游候选）
    for e in edges:
        outs.setdefault(e.src, set()).add(e.dst)
        ins.setdefault(e.dst, set()).add(e.src)
    out: dict[str, int] = {root: 0}
    frontier = [root]
    while frontier:
        nxt: list[str] = []
        for cur in frontier:
            s = out[cur]
            if s <= 0 and s - 1 >= -depth:
                for nb in sorted(outs.get(cur, ())):
                    if nb not in out:
                        out[nb] = s - 1
                        nxt.append(nb)
            if s >= 0 and s + 1 <= depth:
                for nb in sorted(ins.get(cur, ())):
                    if nb not in out:
                        out[nb] = s + 1
                        nxt.append(nb)
        frontier = nxt
    if sides == "upstream":
        return {k: v for k, v in out.items() if v <= 0}
    if sides == "downstream":
        return {k: v for k, v in out.items() if v >= 0}
    return out


def _trim(a: dict, b: dict, mx: float, my: float, gap_end: int = 3,
          gap_start: int = 3) -> tuple[float, float, float, float]:
    """把边端裁到节点圆**之外**，按曲线切线方向（不是弦方向）。

    这是"箭头看不见"的根治：原先圆心→圆心，marker 尖端落在圆心、整枚被不透明节点
    盖住。t=1 的切线是 ``P2-P1``（P1=控制点），t=0 的是 ``P1-P0``。
    """
    dx1, dy1 = b["x"] - mx, b["y"] - my
    n1 = math.hypot(dx1, dy1) or 1.0
    ex = b["x"] - dx1 / n1 * (b["r"] + gap_end)
    ey = b["y"] - dy1 / n1 * (b["r"] + gap_end)
    dx0, dy0 = mx - a["x"], my - a["y"]
    n0 = math.hypot(dx0, dy0) or 1.0
    sx = a["x"] + dx0 / n0 * (a["r"] + gap_start)
    sy = a["y"] + dy0 / n0 * (a["r"] + gap_start)
    return sx, sy, ex, ey


def build(repo, retrieval, settings, spec: dict, *, focus: str = "",
          edge_limit: int = 6000) -> dict:
    """编译一份视图 → 渲染载荷（nodes/links/bands/legend/stats + 回执副本）。"""
    spec = normalize_spec(spec)
    cfg = settings.graph
    edges = repo.citation_edges_all(limit=int(edge_limit))
    foot = {"spec": spec, "views": repo.list_graph_views(),
            "stats": {"edges": len(edges), "src": 0, "dst": 0, "shown": 0,
                      "layers": 0, "edges_shown": 0},
            "nodes": [], "links": [], "bands": [], "columns": [], "legend": [],
            "sides": [], "in_lib_only": bool(spec.get("in_lib_only")),
            "empty": "", "root_found": True, "width": 980, "height": 320,
            "root": spec["root"], "depth": spec["depth"], "label_on": False,
            "layout": spec["layout"], "color_by": spec["color_by"],
            "group_by": spec["group_by"], "badge_on": bool(spec["badge"]),
            "arrow_size": int(spec["arrow_size"])}
    if not edges:
        foot["empty"] = "empty"
        return foot

    tags = repo.tag_map()
    if spec["group_by"] == "tag":
        groups = dict(tags)
        if not spec["group_order"]:                 # 泳道顺序＝产品语义，缺省即因果序
            spec["group_order"] = [t for t in TAG_ORDER if t in set(tags.values())]
    elif spec["group_by"] == "group":
        groups = {k: v for k, v in spec["group_map"].items()}
    else:
        groups = {}
    group_colors = dict(spec["group_colors"])
    if spec["color_by"] == "auto":
        spec["color_by"] = "tag" if (tags and any(t for t in tags.values())) else "kind"

    dst_info: dict[str, dict] = {}
    src_out: dict[str, int] = {}
    for e in edges:
        d = dst_info.setdefault(e.dst_arxiv_id, {"title": e.dst_title, "cites": 0,
                                                 "citations": e.dst_citations,
                                                 "year": int(e.year or 0)})
        d["cites"] += 1
        d["citations"] = max(d["citations"], int(e.dst_citations or 0))
        d["year"] = max(d["year"], int(e.year or 0))
        src_out[e.src_arxiv_id] = src_out.get(e.src_arxiv_id, 0) + 1

    # ⚠ 节点权重**只取结构量（被引数）**，与"在不在库"无关——这是 materialize_view 能收敛的前提：
    # 旧版出边论文入库后权重从 0 变成画像分 ⇒ 选点集合随入库漂移 ⇒ 抓一批漂一批，永远追不上
    # （实测：remaining 卡在 20/78 不动）。画像分仍进 meta（hover 卡照显），但不参与排序。
    cit_of = {d: int(info["citations"] or 0) for d, info in dst_info.items()}

    weights = repo.profile_weights_map()
    years = repo.edge_years()
    nodes_g: list[GNode] = []
    for sid in src_out:
        p = repo.get_paper(sid)
        w = float(cit_of.get(sid, 0)) / 1000.0
        if p is None:                       # 库外引用者（sync_cited_by 反查来）
            nodes_g.append(GNode(id=sid, kind="src", weight=w, meta={
                "kind": "src", "title": sid, "published": "", "in_lib": False,
                "cites": 0, "citations": src_out[sid]}))
            continue
        feat = paper_features(list(p.categories or []), p.primary_category,
                              p.title or "", p.abstract or "", list(p.authors or []))
        sc, why = score_paper(feat, weights)
        pub = p.published_at.date().isoformat() if p.published_at else ""
        years[sid] = years.get(sid) or (int(pub[:4]) if pub[:4].isdigit() else 0)
        nodes_g.append(GNode(id=sid, kind="src", weight=w, meta={
            "kind": "src", "title": p.title or sid, "published": pub,
            "in_lib": True, "sc": sc, "why0": why[0] if why else ""}))
    for did, info in dst_info.items():
        if did in src_out:
            continue
        # ⚠ 在库判断必须**查库**，不能写死 False：只作为 dst 出现的论文也可能已经在库
        # （老 bug：这类点永远显示"未入库"，materialize_view 因此永远收不了口——同一篇被反复"抓"）。
        dl = repo.get_paper(did)
        nodes_g.append(GNode(id=did, kind="dst",
                             weight=float(info["citations"]) / 1000.0, meta={
            "kind": "dst", "title": (dl.title if dl is not None else info["title"]) or did,
            "published": str(info["year"] or ""), "in_lib": dl is not None,
            "cites": info["cites"], "citations": info["citations"]}))
        years.setdefault(did, int(info["year"] or 0))

    gedges = [GEdge(src=e.src_arxiv_id, dst=e.dst_arxiv_id,
                    weight=float(e.dst_citations or 0),
                    kind="cited_by" if (e.direction or "cites") == "cited_by"
                    else ("infl" if e.influential else "")) for e in edges]

    if spec["in_lib_only"]:                      # 纯库内视角：图上的点都在库里
        keep_lib = {n.id for n in nodes_g if n.meta.get("in_lib")}
        nodes_g = [n for n in nodes_g if n.id in keep_lib]
        gedges = [e for e in gedges if e.src in keep_lib and e.dst in keep_lib]
        src_out = {k: v for k, v in src_out.items() if k in keep_lib} or src_out

    # ---------------------------------------------------------------- 分两条路：我的手 / 机器的草稿
    #
    # 为什么不把"放谁、放第几层、层里第几个"继续交给公式：
    # 图画的是**理解**，不是统计。一个网络就几十个点，每个点的位置都可以是一个人的判断——
    # 机器负责的只是"把它画出来"（几何、防重叠、箭头、标签、画布跟着内容长）。
    #
    #   · **curated ＝ 我的手**：`layers='id:层号,…'` 就是内容清单——写下的顺序即层内次序。
    #     点名的才上图，机器不加一个点、不减一个点，不算任何配额。
    #   · **auto ＝ 机器的草稿**：按 root 有向 BFS 捞一圈给我**读一眼再决定**。
    #     它是数据分析的辅助，不是交付物——别拿草稿当成果交差。
    root = spec["root"]
    pin = set(spec["pin"])
    rows: dict[str, int] | None = None
    named_missing: list[str] = []
    if spec["mode"] == "curated":
        manifest = {str(k): int(v) for k, v in (spec["layers"] or {}).items()}
        # 清单＝`layers` 的**键**，只认它：`rank` 只管层内次序、`pin` 是草稿模式的概念——
        # **都不能往清单里加人**。否则从已发布视图继承来的 pin/rank 会悄悄塞进你没点名的点
        # （实测踩过：清单 16 篇，图上冒出第 17 篇——旧视图的 pin 带来的）。
        order_ids = [i for i in (spec["rank"] or []) if i in manifest]
        order_ids += [i for i in manifest if i not in set(order_ids)]
        by_id = {n.id: n for n in nodes_g}
        kept: list[GNode] = []
        for ident in order_ids:
            node = by_id.get(ident)
            if node is None:                       # 图里没它（边没织过）⇒ 拿库里的记录补一个孤点
                p = repo.get_paper(ident)
                if p is None:
                    named_missing.append(ident)    # 库里也没有：**响亮点名**，别静默吞掉
                    continue
                pub = p.published_at.date().isoformat() if p.published_at else ""
                node = GNode(id=ident, kind="src", weight=0.0, meta={
                    "kind": "src", "title": p.title or ident, "published": pub,
                    "in_lib": True, "cites": 0, "citations": 0})
            kept.append(node)
        nodes_g = kept
        keep = set(manifest)
        gedges = [e for e in gedges if e.src in keep and e.dst in keep]
        top = max(manifest.values()) if manifest else 0
        rows = {nid: (top - s) for nid, s in manifest.items()}
        sources = {root} if (root and root in keep) else (set(keep) or {root})
        order_map = {aid: i for i, aid in enumerate(order_ids) if aid in keep}
    elif root:
        if not any(n.id == root for n in nodes_g):
            foot["root_found"] = False
            foot["stats"] = {**foot["stats"], "src": len(src_out), "dst": len(dst_info)}
            return foot
        signed = _signed_depths(gedges, root, int(spec["depth"]), spec["sides"])
        # 草稿上也可以点名纠层（显式意图 > 拓扑距离），只认图里确实有边的 id
        known = {n.id for n in nodes_g}
        for ident, lv in (spec.get("layers") or {}).items():
            if ident in known:
                signed[ident] = int(lv)
        keep = set(signed)
        nodes_g = [n for n in nodes_g if n.id in keep]
        gedges = [e for e in gedges if e.src in keep and e.dst in keep]
        # 行号：下游（s>0）行号小 ⇒ 画在下方；上游（s<0）行号大 ⇒ 画在上方；根居中
        top = max(signed.values())
        rows = {nid: (top - s) for nid, s in signed.items()}
        sources = {root}
        pin.add(root)
    else:
        sources = set(src_out)

    # ---------------------------------------------------------------- 编排
    budget: dict[int, int] | None = None
    if spec["mode"] == "curated":
        # 清单模式：层里有几个就画几个（机器不加不减）⇒ max_nodes 不再参与裁剪
        budget = {}
        for _nid, r in (rows or {}).items():
            budget[r] = budget.get(r, 0) + 1
        max_nodes = len(nodes_g)
    else:
        # 草稿模式：rank 列表的顺序即"离中心多近"（序号 0 最近）
        order_map = {aid: i for i, aid in enumerate(spec["rank"] or [])}
        max_nodes = int(spec["max_nodes"])
    common = dict(max_nodes=max_nodes, sort_within=spec["sort_within"],
                  size_by=spec["size_by"], pin=pin, groups=groups or None,
                  group_order=spec["group_order"], group_quota=int(spec["group_quota"]))
    if spec["layout"] == "timeline":
        lay = timeline_layout(nodes_g, gedges, sources=sources, years=years,
                              col_gap=int(spec["layer_gap"]), row_gap=int(spec["node_gap"]),
                              **common)
    else:
        lay = layered_layout(nodes_g, gedges, sources=sources,
                             layer_gap=int(spec["layer_gap"]),
                             node_gap=int(spec["node_gap"]),
                             order_map=order_map or None,
                             center_out=spec["place"] == "center",
                             layer_budget=budget,
                             depths=rows, **common)

    # 侧标注（上游在上 / 下游在下）：**只在分层布局下成立**——年代布局的"层"是年份列，
    # 拿它标上游/下游是语义错位（实测边界：timeline + root 会冒出 25 个"上游"标签）。
    sides: list[dict] = []
    if rows and spec["layout"] == "layer":
        row_y = {p["layer"]: p["y"] for p in lay["pos"].values()}
        root_row = rows[root]
        for r in sorted(row_y):
            if r == root_row:
                label, color = "本体", "#0f172a"
            elif r > root_row:
                label, color = "上游 · 它引用的", "#64748b"
            else:
                label, color = "下游 · 引用它的", "#dc2626"
            sides.append({"y": row_y[r], "label": label, "color": color, "row": r})

    meta_by = {n.id: n.meta for n in nodes_g}
    shown = len(lay["pos"])
    label_on = spec["label_mode"] == "always" or (
        spec["label_mode"] == "auto" and 0 < shown <= int(spec["label_auto_max"]))

    nodes: list[dict] = []
    for nid, pt in lay["pos"].items():
        m = meta_by.get(nid) or {}
        tag = tags.get(nid, "")
        grp = groups.get(nid, "") if groups else ""
        fill = _resolve_color(spec["color_by"], nid, tag=tag, group=grp,
                              in_lib=bool(m.get("in_lib")), weight=float(
                                  next((n.weight for n in nodes_g if n.id == nid), 0.0)),
                              group_colors=group_colors)
        label = short_id(nid, max_len=int(spec["label_max"])) if spec["label_style"] == "id" \
            else label_for(m.get("title", ""), nid, max_len=int(spec["label_max"]))
        nodes.append({**pt, "id": nid, "is_root": nid == root, "tag": tag, "group": grp,
                      "fill": fill, "label": label, "in_lib": bool(m.get("in_lib")),
                      "badge": (tag or grp)[:2] if spec["badge"] and (tag or grp) else "",
                      "card": _card(retrieval, nid, m),
                      "show_label": label_on,
                      "year": years.get(nid, 0)})

    # ---------------------------------------------------------------- 边（裁到圆外 + 箭头）
    pos = lay["pos"]
    keep_edges = [le for le in lay["edges"] if le["keep"]]
    if len(keep_edges) > int(spec["max_edges"]):
        keep_edges = sorted(keep_edges, key=lambda le: -le["weight"])[:int(spec["max_edges"])]
    links: list[dict] = []
    for i, lnk in enumerate(keep_edges):
        a, b = pos.get(lnk["src"]), pos.get(lnk["dst"])
        if not (a and b):
            continue
        bend = ((i % 5) - 2) * 16                          # 二次曲线错开，防重叠成一束
        mx, my = (a["x"] + b["x"]) / 2 + bend, (a["y"] + b["y"]) / 2
        sx, sy, ex, ey = _trim(a, b, mx, my, gap_end=int(spec["arrow_size"]) // 3 + 1)
        dim = bool(focus) and focus not in (lnk["src"], lnk["dst"])
        links.append({"d": f"M {sx:.0f} {sy:.0f} Q {mx:.0f} {my:.0f} {ex:.0f} {ey:.0f}",
                      "src": lnk["src"], "dst": lnk["dst"],
                      "x1": int(sx), "y1": int(sy), "x2": int(ex), "y2": int(ey),
                      "dim": dim, "infl": lnk["kind"] == "infl",
                      "cited_by": lnk["kind"] == "cited_by",
                      "weight": lnk["weight"]})

    # ---------------------------------------------------------------- 分组带 / 年份列 / 图例
    bands = []
    for bd in lay.get("bands", []):
        g = bd["group"]
        if g == "" and spec["group_by"] == "none":
            continue
        color = (group_colors.get(g) or (TAG_COLORS.get(g) if spec["group_by"] == "tag"
                                         else None)
                 or ("#94a3b8" if g == "" else GROUP_PALETTE[0]))
        bands.append({**bd, "color": color, "group": g or "其他上游"})
    legend = [] if not tags else [{"tag": t, "color": TAG_COLORS.get(t, "#94a3b8"),
                                   "count": sum(1 for t2 in tags.values() if t2 == t)}
                                  for t in TAG_ORDER if any(t2 == t for t2 in tags.values())]

    foot.update({
        "nodes": nodes, "links": links, "bands": bands, "legend": legend,
        "columns": lay.get("columns", []), "sides": sides,
        "width": lay["width"], "height": lay["height"],
        "root": root, "depth": spec["depth"], "label_on": label_on,
        "layout": spec["layout"], "color_by": spec["color_by"],
        "group_by": spec["group_by"], "badge_on": bool(spec["badge"]),
        "arrow_size": int(spec["arrow_size"]), "spec": spec,
        "stats": {"edges": len(edges), "edges_shown": len(links),
                  "src": len(src_out), "dst": len(dst_info),
                  "shown": shown, "layers": lay["layers"], "mode": spec["mode"],
                  "named_missing": named_missing,
                  "in_lib": sum(1 for n in nodes if n.get("in_lib")),
                  "not_in_lib": sum(1 for n in nodes if not n.get("in_lib"))},
    })
    return foot
