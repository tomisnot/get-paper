"""图底座（F1 数据模型 + F2 分层布局）：纯函数、零 IO、确定性。

域层不携带任何领域词汇（判据 G5 扫描本文件）——Node/Edge 只是几何与拓扑，
语义由消费方适配器给（每个图实例各写各的转换）。
不做力导向（抖动破坏可复算）；层数不是配置项，是拓扑属性。
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    id: str                                  # 任意实体键（arxiv_id 等）
    kind: str = "entity"                     # 呈现分类，由消费方定义
    label: str = ""
    weight: float = 0.0                      # 主权重（语义留给消费方）
    meta: dict = field(default_factory=dict)  # 领域负载（title/published/in_lib…）


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    weight: float = 1.0
    kind: str = ""


def _neighbours(edges: list[Edge]):
    inc: Counter[str] = Counter()
    outd: Counter[str] = Counter()
    nbr: dict[str, set[str]] = {}
    for e in edges:
        outd[e.src] += 1
        inc[e.dst] += 1
        nbr.setdefault(e.src, set()).add(e.dst)
        nbr.setdefault(e.dst, set()).add(e.src)
    return inc, outd, nbr


def layer_depths(nodes: list[Node], edges: list[Edge],
                 sources: set[str]) -> dict[str, int]:
    """层号 = 从源集出发的最短路径深度（BFS）。到不了的节点不出现在结果里。"""
    _, _, nbr = _neighbours(edges)
    ids = {n.id for n in nodes}
    depth: dict[str, int] = {s: 0 for s in sorted(sources) if s in ids}
    queue = deque(depth)
    while queue:
        cur = queue.popleft()
        for nb in sorted(nbr.get(cur, ())):
            if nb not in depth:
                depth[nb] = depth[cur] + 1
                queue.append(nb)
    return depth


def layered_layout(nodes: list[Node], edges: list[Edge], *, sources: set[str],
                   layer_gap: int = 130, node_gap: int = 90,
                   max_nodes: int = 40, sort_within: str = "weight",
                   size_by: str = "degree") -> dict:
    """确定性分层坐标，**画布跟着内容长**。

    实测教训（用户截图）：固定 980px 宽 + 固定小高 ⇒ 一层三十个节点叠成饼、
    标签糊成一团。现在：每层间距 ≥ 该层最大直径+余量（几何上永不重叠），总宽取
    最挤层所需；层内标签奇偶错峰（stag）；返回 width/height 供消费方直接开画。
    超 max_nodes ⇒ 每层按入度/权重截 top-K（tie 按 id）；层数 >6 只留相邻层边。
    确定性：同输入（含乱序）同输出。
    """
    inc, outd, _ = _neighbours(edges)
    depth = layer_depths(nodes, edges, sources)
    weight_of = {n.id: n.weight for n in nodes}
    meta_of = {n.id: n.meta for n in nodes}

    def key(n: Node):
        if sort_within == "year":
            return (str(meta_of.get(n.id, {}).get("published") or ""),
                    -inc.get(n.id, 0), n.id)
        return (-inc.get(n.id, 0), -weight_of.get(n.id, 0.0), n.id)

    def radius(nid: str) -> int:
        if size_by == "weight":
            return min(24, 8 + int(max(0.0, weight_of.get(nid, 0.0)) * 5))
        if size_by == "flat":
            return 10
        return min(24, 8 + inc.get(nid, 0) * 2)

    by_layer: dict[int, list[Node]] = {}
    for n in nodes:
        if n.id in depth:
            by_layer.setdefault(depth[n.id], []).append(n)
    layers = (max(by_layer) + 1) if by_layer else 0
    per_layer = max(4, -(-max_nodes // max(1, layers)))          # ceil 配额
    picked: dict[int, list[Node]] = {}
    total = 0
    for d, lst in sorted(by_layer.items()):
        lst = sorted(lst, key=key)
        if sort_within == "year":
            lst = list(reversed(lst))                            # 新→旧
        picked[d] = lst[:per_layer]
        total += len(picked[d])
    for d in sorted(picked, reverse=True):
        if total <= max_nodes:
            break
        cut = picked[d][-(total - max_nodes):]
        total -= len(cut)
        picked[d] = picked[d][:len(picked[d]) - len(cut)]
    if not picked:
        return {"pos": {}, "layers": 0, "edges": [], "width": 980, "height": 460}

    # 自适应宽度：每层间距 ≥ 该层最大直径+18，总宽取最挤层所需（下限 980）
    spacing: dict[int, int] = {}
    width = 980
    for d, lst in picked.items():
        rmax = max(radius(n.id) for n in lst)
        spacing[d] = max(int(node_gap), 2 * rmax + 18)
        need = spacing[d] * (len(lst) - 1) + 2 * rmax + 140
        width = max(width, need)
    max_d = max(picked)
    base_y = 70 + max_d * layer_gap                              # 源层（深度 0）在底部
    pos: dict[str, dict] = {}
    for d, lst in picked.items():
        sp = spacing[d]
        n = len(lst)
        x0 = (width - sp * (n - 1)) / 2
        y = base_y - d * layer_gap
        for i, nd in enumerate(lst):
            pos[nd.id] = {"x": int(x0 + i * sp), "y": int(y), "layer": d,
                          "r": radius(nd.id), "in_deg": inc.get(nd.id, 0),
                          "stag": 18 if i % 2 else 0}            # 标签错峰防叠字
    out_edges = []
    adjacent_only = layers > 6
    for e in edges:
        a, b = pos.get(e.src), pos.get(e.dst)
        if not (a and b):
            continue
        keep = (abs(a["layer"] - b["layer"]) == 1) if adjacent_only else True
        out_edges.append({"src": e.src, "dst": e.dst, "kind": e.kind,
                          "weight": e.weight, "keep": keep,
                          "span": abs(a["layer"] - b["layer"])})
    height = 170 + max_d * layer_gap + 60                        # 上下留标签与错峰
    return {"pos": pos, "layers": layers, "edges": out_edges,
            "width": int(width), "height": int(height)}


def label_for(title: str, ident: str, *, max_len: int = 18) -> str:
    """呈现参数，不是页面私货：截断长度由消费配置给（判据 G4），全称留给 hover 卡。"""
    text = (title or ident).strip()
    if max_len > 3 and len(text) > max_len:
        return text[:max_len - 1] + "…"
    return text
