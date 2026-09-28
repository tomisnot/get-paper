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
                   width: int = 980, layer_gap: int = 110, node_gap: int = 90,
                   max_nodes: int = 60, sort_within: str = "weight",
                   size_by: str = "degree") -> dict:
    """确定性分层坐标。返回 {pos:{id:{x,y,layer,r,in_deg}}, layers, edges:[{...,keep}]}。

    - 源层在底部，深度越大越靠上；层内按 sort_within 排序、等距散布；
    - 总节点数超 max_nodes ⇒ 每层按入度/权重截 top-K（tie 按 id，可复算）；
    - 层数 >6 ⇒ 只画相邻层边（防蜘蛛网），跨层边在渲染层折叠。
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
        take = lst[:per_layer]
        picked[d] = take
        total += len(take)
    # 若仍超预算，从最大层开始砍尾部
    for d in sorted(picked, reverse=True):
        if total <= max_nodes:
            break
        cut = picked[d][-(total - max_nodes):]
        total -= len(cut)
        picked[d] = picked[d][:len(picked[d]) - len(cut)]
    if not picked:
        return {"pos": {}, "layers": 0, "edges": []}
    max_d = max(picked)
    base_y = 60 + max_d * layer_gap                              # 源层（深度0）在底部
    pos: dict[str, dict] = {}
    for d, lst in picked.items():
        n = len(lst)
        span = min(width - 100, max(node_gap, 1) * (n - 1))
        x0 = (width - span) / 2
        y = base_y - d * layer_gap
        for i, nd in enumerate(lst):
            deg = inc.get(nd.id, 0)
            if size_by == "degree":
                r = min(22, 6 + deg * 3)
            elif size_by == "weight":
                r = min(22, 7 + int(max(0.0, weight_of.get(nd.id, 0.0)) * 5))
            else:                                                # flat
                r = 9
            pos[nd.id] = {"x": int(x0 + (span * i / max(1, n - 1) if n > 1 else span / 2)),
                          "y": int(y), "layer": d, "r": r, "in_deg": deg}
    laid = [(e, pos.get(e.src), pos.get(e.dst)) for e in edges]
    out_edges = []
    adjacent_only = layers > 6
    for e, a, b in laid:
        if not (a and b):
            continue
        keep = (abs(a["layer"] - b["layer"]) == 1) if adjacent_only else True
        out_edges.append({"src": e.src, "dst": e.dst, "kind": e.kind,
                          "weight": e.weight, "keep": keep, "span": abs(a["layer"] - b["layer"])})
    return {"pos": pos, "layers": layers, "edges": out_edges}


def label_for(title: str, ident: str, *, max_len: int = 18) -> str:
    """呈现参数，不是页面私货：截断长度由消费配置给（判据 G4），全称留给 tooltip。"""
    text = (title or ident).strip()
    if max_len > 3 and len(text) > max_len:
        return text[:max_len - 1] + "…"
    return text
