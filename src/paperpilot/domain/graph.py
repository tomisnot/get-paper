"""图底座（F1 数据模型 + F2 编排布局）：纯函数、零 IO、确定性。

域层不携带任何领域词汇（判据 G5 扫描本文件）——Node/Edge 只是几何与拓扑，
语义由消费方适配器给（每个图实例各写各的转换）。
不做力导向（抖动破坏可复算）；层数不是配置项，是拓扑属性。

两种编排（同一 Node/Edge 契约、同一返回结构，消费方按视图的 layout 选）：
- ``layered_layout``：y＝拓扑深度、x＝层内序号（默认；分组时同组聚簇 + 出 band 标题）；
- ``timeline_layout``：x＝年份列、y＝列内序号（看"年代脉络"用；缺年份进"未知"列）。
两者都支持 ``pin``（锚点必留，永不截断）与 ``group_quota``（每组保底），
让编排服从叙事，而不只服从度数。
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

#: 未分组节点的组键（不参与"每组保底"抢占，排在各显式组之前）
UNGROUPED = ""


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


def _group_rank(groups: dict[str, str] | None,
                group_order: list[str] | None) -> dict[str, int]:
    """组 → 序号：显式 group_order 优先，未列出的组按名字排在后面（确定性）。"""
    if not groups:
        return {}
    order = list(group_order or [])
    extra = sorted({g for g in groups.values()} - set(order))
    return {g: i for i, g in enumerate([*order, *extra])}


def _rank_of(rank: dict[str, int], gid: str) -> int:
    """未分组/未知组排在**所有显式组之后**——叙事视角下"有分类的"才该占可见预算，
    否则一大坨灰色未分类节点会凭度数把六色故事挤出画面（实测截图）。"""
    return rank.get(gid, len(rank))


def _pick_layer(ordered: list[Node], quota: int, *, pin: set[str],
                groups: dict[str, str] | None, group_quota: int, key) -> list[Node]:
    """层/列内选点：锚点必留 → 每组保底 → 按 key 补满；超额时只砍非锚点。"""
    if not pin and group_quota <= 0:
        return ordered[:quota]
    keep: list[Node] = []
    seen: set[str] = set()

    def take(n: Node) -> None:
        if n.id not in seen:
            seen.add(n.id)
            keep.append(n)

    for n in ordered:                                  # ① 锚点必留
        if n.id in pin:
            take(n)
    if group_quota > 0 and groups:                     # ② 每组保底（未分组不抢配额）
        got: Counter[str] = Counter()
        for n in [x for x in ordered if x.id not in seen]:
            gid = groups.get(n.id, UNGROUPED)
            if gid == UNGROUPED or got[gid] >= group_quota:
                continue
            got[gid] += 1
            take(n)
    for n in ordered:                                  # ③ 按 key 补满
        if len(keep) >= quota:
            break
        take(n)
    keep.sort(key=key)
    if len(keep) > quota:                              # ④ 超额只砍非锚点
        pinned = [n for n in keep if n.id in pin]
        rest = [n for n in keep if n.id not in pin]
        keep = sorted(pinned + rest[:max(0, quota - len(pinned))], key=key)
    return keep


def _trim_total(picked: dict, total: int, max_nodes: int, pin: set[str]) -> tuple[dict, int]:
    """全局超额：从最深层/最右列往回砍，锚点不动。"""
    for d in sorted(picked, reverse=True):
        if total <= max_nodes:
            break
        droppable = [n for n in picked[d] if n.id not in pin]
        need = total - max_nodes
        if not droppable or need <= 0:
            continue
        cut_ids = {n.id for n in droppable[-need:]}
        total -= len(cut_ids)
        picked[d] = [n for n in picked[d] if n.id not in cut_ids]
    return picked, total


def _center_slots(n: int) -> list[int]:
    """行内槽位（``0..n-1`` 的一个**排列**），按"离行中轴由近及远"排序。

    ⚠ 必须返回**排列**，不能返回无界偏移：消费方按 ``x0 + slot*sp`` 定点，而 ``x0`` 只按
    ``n-1`` 个间隔铺开——偏移一旦超过 ``(n-1)/2``，节点就被**推出画布**。旧版写成
    ``j if j % 2 else -j``（幅度随 j 线性增长、索引 0 还向右偏），实测把 12 个点的行排到
    ``x=-156 … 1544`` 而画布宽仅 1288：点跑出框外、边被拽长，"骨干居中"反而更糟。
    """
    mid = (n - 1) / 2
    return sorted(range(n), key=lambda i: (abs(i - mid), i))


def _bands(picked: dict, pos: dict, groups: dict[str, str] | None) -> list[dict]:
    """层/列内**连续同组**段 → band（供消费方画分组标题）。未分组时返回空。

    ⚠ 按**画面上的 x 顺序**取连续段，不按选点列表顺序——``center_out`` 排布时
    列表顺序≠视觉顺序，照列表算会把带子画错位置（实测踩过）。
    """
    if not groups:
        return []
    out: list[dict] = []
    for d, lst in sorted(picked.items()):
        visual = sorted(lst, key=lambda n: pos[n.id]["x"])
        run: list[Node] = []
        for n in [*visual, None]:                       # 哨兵收尾
            gid = UNGROUPED if n is None else groups.get(n.id, UNGROUPED)
            if run and gid != groups.get(run[0].id, UNGROUPED):
                xs = [pos[m.id]["x"] for m in run]
                rs = [pos[m.id]["r"] for m in run]
                out.append({"layer": d, "group": groups.get(run[0].id, UNGROUPED),
                            "x0": min(xs) - max(rs) - 8, "x1": max(xs) + max(rs) + 8,
                            "y": pos[run[0].id]["y"], "n": len(run)})
                run = []
            if n is not None:
                run.append(n)
    return out


def _align_rows(picked: dict, edges: list[Edge], groups: dict[str, str] | None,
                anchor: int, prio: dict[str, int] | None = None,
                sweeps: int = 2) -> dict:
    """**按连接重心重排各行**（Sugiyama 重心法）：让"有引用关系的点"在相邻行上下对齐。

    为什么需要：行内默认按"分组 + 度数"排，**完全无视连接关系**——一条边可能横穿整幅画
    （实测 1288px 画布上出现 861px 的横跨），画面就显得是程序平铺出来的。

    分工（可组合，不互斥）：
    - ``prio``（＝视图的 rank）：点名的骨干**按重要性排在该行最前**——配合 ``center_out``
      由它决定"谁贴中轴"；其余节点才参与重心对齐。这样"我的判断"与"连接关系"各管一段，
      而不是二者只能选一个。
    - ⚠ 重心对齐是**块级**而非点级：``groups`` 给了就把同组视作一个块整体移动，否则泳道
      会被打散成碎片（分组带随之碎掉）。块的分数＝块内成员在"已定位行"中邻居的序号均值；
      没有对外连接的块保持原相对次序、垫在其后（确定性）。``sweeps`` 多轮让对齐跨行传播。
    """
    nbr: dict[str, set[str]] = {}
    for e in edges:
        nbr.setdefault(e.src, set()).add(e.dst)
        nbr.setdefault(e.dst, set()).add(e.src)
    row_of = {n.id: d for d, lst in picked.items() for n in lst}
    idx: dict[str, int] = {}
    rows = sorted(picked, key=lambda d: (abs(d - anchor), d))     # 从锚点行向外传播
    prio = prio or {}
    for _ in range(sweeps):
        for d in rows:
            lst = picked.get(d) or []
            if not lst:
                continue
            top: list[Node] = []                                  # 我点名的骨干（按重要性）
            rest: list[Node] = []
            for n in lst:
                (top if n.id in prio else rest).append(n)
            if prio:
                top.sort(key=lambda n: (prio[n.id], n.id))
            if groups:                                            # 同组连成块（保泳道）
                where: dict[str, int] = {}
                blocks: list[list[Node]] = []
                for n in rest:
                    g = groups.get(n.id, UNGROUPED)
                    if g in where:
                        blocks[where[g]].append(n)
                    else:
                        where[g] = len(blocks)
                        blocks.append([n])
            else:
                blocks = [[n] for n in rest]

            def score(bl: list[Node], row: int = d) -> float | None:
                """块分＝成员在"已定位行"里邻居的序号均值（**排除自己所在行**，否则自我印证）。

                ⚠ `row: int = d` 这个默认参数是**刻意的**：在循环里定义的闭包若直接读 `d`，
                等循环结束后再调用会拿到最后一轮的 `d`（B023 晚绑定）。这里虽然当场就调用、
                不会出错，但显式绑定把"这个块属于哪一行"钉死，读代码与将来重构都安全。
                """
                vals = [idx[m] for n in bl for m in sorted(nbr.get(n.id, ()))
                        if m in idx and row_of.get(m) != row]
                return sum(vals) / len(vals) if vals else None

            scored = [(score(b), i, b) for i, b in enumerate(blocks)]
            linked = sorted((s for s in scored if s[0] is not None), key=lambda s: (s[0], s[1]))
            loose = [s for s in scored if s[0] is None]
            flat = [*top, *(n for _sc, _i, bl in [*linked, *loose] for n in bl)]
            picked[d] = flat
            for i, n in enumerate(flat):
                idx[n.id] = i
    return picked


def _plan_seats(by_layer: dict[int, list[Node]], max_nodes: int, key) -> dict[int, int]:
    """草稿模式的各层席位：**每层先保底 1，其余按全局重要性补满**。

    只此一条规则——没有比例、没有上限、没有取整公式。因为这一路本来就是"机器替我捞一圈，
    让我先看一眼"；真正的取舍该由我**点名**（``layer_budget`` 那条路＝我的清单）。

    历史上这里换过三版（均匀 ceil ⇒ 按候选数比例 ⇒ 比例加 2× 上限），每一版都在替调用方
    做判断：均匀版把每层切成同一个数（用户一眼看出"这是算出来的"），比例版让某层独吞
    26/38 个名额把库外噪声拉进画面。教训：**选点是不该由公式代劳的判断**。
    """
    live = {d: lst for d, lst in by_layer.items() if lst}
    if not live:
        return {}
    seats = {d: 1 for d in live}                    # 每层至少 1：有候选就不断层
    left = max(0, int(max_nodes) - len(seats))
    if left <= 0:
        return seats
    layer_of = {n.id: d for d, lst in live.items() for n in lst}
    granted: set[int] = set()
    for n in sorted((n for lst in live.values() for n in lst), key=key):
        if left <= 0:
            break
        d = layer_of[n.id]
        if d not in granted:                        # 该层的保底名额已经算过了
            granted.add(d)
            continue
        seats[d] += 1
        left -= 1
    return seats



def _edge_rows(pos: dict, edges: list[Edge], layers: int,
               *, drop_long: bool = True) -> list[dict]:
    """边行。``drop_long``：层数 >6 时只留相邻层（防深度图变蜘蛛网）——**仅对深度分层成立**；
    年代列不是拓扑层，年份跨度大也得留（否则"跨年的引用"全被砍，实测只剩 11 条边）。"""
    out = []
    adjacent_only = drop_long and layers > 6
    for e in edges:
        a, b = pos.get(e.src), pos.get(e.dst)
        if not (a and b):
            continue
        keep = (abs(a["layer"] - b["layer"]) == 1) if adjacent_only else True
        out.append({"src": e.src, "dst": e.dst, "kind": e.kind,
                    "weight": e.weight, "keep": keep,
                    "span": abs(a["layer"] - b["layer"])})
    return out


def _radius_of(size_by: str, nid: str, inc: Counter, weight_of: dict) -> int:
    if size_by == "weight":
        return min(24, 8 + int(max(0.0, weight_of.get(nid, 0.0)) * 5))
    if size_by == "flat":
        return 10
    return min(24, 8 + inc.get(nid, 0) * 2)


def layered_layout(nodes: list[Node], edges: list[Edge], *, sources: set[str],
                   layer_gap: int = 130, node_gap: int = 90,
                   max_nodes: int = 40, sort_within: str = "weight",
                   size_by: str = "degree", pin: set[str] | None = None,
                   groups: dict[str, str] | None = None,
                   group_order: list[str] | None = None,
                   group_quota: int = 0,
                   depths: dict[str, int] | None = None,
                   order_map: dict[str, int] | None = None,
                   center_out: bool = False,
                   layer_budget: dict[int, int] | None = None) -> dict:
    """确定性分层坐标，**画布跟着内容长**（y＝行号、x＝行内序号）。

    语义分工（可自定义的关键）：
    - ``depths``：**调用方自带的层号**（行索引，≥0）——给了就不再自己 BFS。这样"层"
      的语义（引用方向上的上游/下游、年代、任意自定义分层）由消费方决定，域层只画几何
      （G5：本文件不带领域词）。缺省仍是从 ``sources`` 出发的无向最短路径深度。
    - ``order_map``：**调用方的显式优先级**（id → 序号，小的在前）——给了就**压过**度数与分组：
      它表达"这几篇是我认定的骨干"，而不是"按被引数排"。未列入的按原 key 排在其后。
    - ``center_out``：行内**从中心向两侧展开**（序号 0 落在该行中点）——骨干贴近纵向中轴，
      连线短、重心稳；不分组时等价于"重要的事摆中间"。
    - ``pin``：锚点（如根节点）永不截断——层内超额与全局超额都只砍非锚点；
    - ``groups``：层内按组聚簇（同组连续 ⇒ 出 ``bands``，可画泳道标题）；
    - ``group_quota``：截断时每组至少留几篇，让叙事里的少数派不被高被引挤掉；
    - ``layer_budget``：**各层席位数**（层号 → 席位）——**清单模式**：调用方点名了几个就画几个，
      机器不加不减（对应视图的 curated 模式）。不给则走草稿模式（每层保底 1、按重要性补满）。

    实测教训（用户截图）：固定 980px 宽 + 固定小高 ⇒ 一层三十个节点叠成饼、
    标签糊成一团。现在：每层间距 ≥ 该层最大直径+余量（几何上永不重叠），总宽取
    最挤层所需；层内标签奇偶错峰（stag）；返回 width/height 供消费方直接开画。
    超 max_nodes ⇒ 每层按入度/权重截 top-K（tie 按 id）；层数 >6 只留相邻层边。
    确定性：同输入（含乱序）同输出。
    """
    pin = set(pin or ())
    inc, outd, _ = _neighbours(edges)
    depth = {k: int(v) for k, v in depths.items()} if depths is not None \
        else layer_depths(nodes, edges, sources)
    weight_of = {n.id: n.weight for n in nodes}
    meta_of = {n.id: n.meta for n in nodes}
    rank = _group_rank(groups, group_order)
    prio = {str(k): int(v) for k, v in (order_map or {}).items()}

    def key(n: Node):
        g = _rank_of(rank, groups.get(n.id, UNGROUPED)) if groups else 0
        explicit = (0, prio[n.id]) if n.id in prio else (1, 0)   # 显式优先级压过度数/分组
        if sort_within == "year":
            return (*explicit, g, str(meta_of.get(n.id, {}).get("published") or ""),
                    -inc.get(n.id, 0), n.id)
        return (*explicit, g, -inc.get(n.id, 0), -weight_of.get(n.id, 0.0), n.id)

    by_layer: dict[int, list[Node]] = {}
    for n in nodes:
        if n.id in depth:
            by_layer.setdefault(depth[n.id], []).append(n)
    layers = (max(by_layer) + 1) if by_layer else 0
    # 各层席位有两条路，泾渭分明：
    #   · ``layer_budget`` 给了 ⇒ **清单模式**：调用方点名了几个就画几个，机器不加不减（我的笔）；
    #   · 没给 ⇒ **草稿模式**：每层保底 1、其余按全局重要性补满（机器替我捞一圈，供我读一眼）。
    if layer_budget is not None:
        quotas = {d: max(0, int(layer_budget.get(d, 0))) for d in by_layer}
    else:
        quotas = _plan_seats(by_layer, max_nodes, key)
    picked: dict[int, list[Node]] = {}
    total = 0
    for d, lst in sorted(by_layer.items()):
        ordered = sorted(lst, key=key)
        if sort_within == "year" and not groups:
            ordered = list(reversed(ordered))                    # 新→旧（分组时不倒，组内成块）
        picked[d] = _pick_layer(ordered, quotas.get(d, 0), pin=pin, groups=groups,
                                group_quota=group_quota, key=key)
        total += len(picked[d])
    picked, total = _trim_total(picked, total, max_nodes, pin)
    if sort_within == "align":                                   # 行内按连接重心对齐
        src_rows = [depth[s] for s in sorted(sources) if s in depth]
        anchor = min(src_rows) if src_rows else (min(picked) if picked else 0)
        picked = _align_rows(picked, edges, groups, anchor, prio=prio)
    if not any(picked.values()):
        return {"pos": {}, "layers": 0, "edges": [], "width": 980, "height": 460,
                "bands": [], "columns": []}

    # 自适应宽度：每层间距 ≥ 该层最大直径+18，总宽取最挤层所需（下限 980）
    spacing: dict[int, int] = {}
    width = 980
    for d, lst in picked.items():
        if not lst:
            continue
        rmax = max(_radius_of(size_by, n.id, inc, weight_of) for n in lst)
        spacing[d] = max(int(node_gap), 2 * rmax + 18)
        need = spacing[d] * (len(lst) - 1) + 2 * rmax + 140
        width = max(width, need)
    max_d = max(picked)
    base_y = 70 + max_d * layer_gap                              # 源层（深度 0）在底部
    pos: dict[str, dict] = {}
    for d, lst in picked.items():
        if not lst:
            continue
        sp = spacing[d]
        n = len(lst)
        x0 = (width - sp * (n - 1)) / 2
        y = base_y - d * layer_gap
        slots = _center_slots(n) if center_out else list(range(n))
        for i, nd in enumerate(lst):
            pos[nd.id] = {"x": int(x0 + slots[i] * sp), "y": int(y), "layer": d,
                          "r": _radius_of(size_by, nd.id, inc, weight_of),
                          "in_deg": inc.get(nd.id, 0),
                          "stag": 18 if i % 2 else 0}            # 标签错峰防叠字
    height = 170 + max_d * layer_gap + 60                        # 上下留标签与错峰
    return {"pos": pos, "layers": layers, "edges": _edge_rows(pos, edges, layers),
            "width": int(width), "height": int(height),
            "bands": _bands(picked, pos, groups), "columns": []}


def timeline_layout(nodes: list[Node], edges: list[Edge], *, sources: set[str],
                    years: dict[str, int] | None = None, col_gap: int = 150,
                    row_gap: int = 90, max_nodes: int = 40,
                    sort_within: str = "weight", size_by: str = "degree",
                    pin: set[str] | None = None, groups: dict[str, str] | None = None,
                    group_order: list[str] | None = None,
                    group_quota: int = 0, unknown_year: int = 0) -> dict:
    """**年代编排**：x＝年份列（升序，缺年份进最左"未知"列）、y＝列内序号。

    看"脉络"用：横轴自己把源头与下游拉开，不必读层号。返回结构与
    ``layered_layout`` 一致（``layer`` 字段＝年份列号，``columns`` 给列标题），
    消费方模板零改动即可切换。
    """
    pin = set(pin or ())
    years = dict(years or {})
    inc, _, _ = _neighbours(edges)
    depth = layer_depths(nodes, edges, sources)
    weight_of = {n.id: n.weight for n in nodes}
    meta_of = {n.id: n.meta for n in nodes}
    rank = _group_rank(groups, group_order)

    def key(n: Node):
        g = _rank_of(rank, groups.get(n.id, UNGROUPED)) if groups else 0
        return (g, -inc.get(n.id, 0), -weight_of.get(n.id, 0.0), n.id)

    def year_of(n: Node) -> int:
        y = int(years.get(n.id) or 0)
        if not y:
            raw = str(meta_of.get(n.id, {}).get("published") or "")
            y = int(raw[:4]) if raw[:4].isdigit() else 0
        return y or unknown_year

    cols: dict[int, list[Node]] = {}
    for n in nodes:
        if n.id in depth:
            cols.setdefault(year_of(n), []).append(n)
    col_ids = sorted(cols)
    per_col = max(4, -(-max_nodes // max(1, len(col_ids) or 1)))
    picked: dict[int, list[Node]] = {}
    total = 0
    for yi, cid in enumerate(col_ids):
        picked[yi] = _pick_layer(sorted(cols[cid], key=key), per_col, pin=pin,
                                 groups=groups, group_quota=group_quota, key=key)
        total += len(picked[yi])
    picked, total = _trim_total(picked, total, max_nodes, pin)
    if not any(picked.values()):
        return {"pos": {}, "layers": 0, "edges": [], "width": 980, "height": 460,
                "bands": [], "columns": []}

    spacing: dict[int, int] = {}
    height = 460
    for yi, lst in picked.items():
        if not lst:
            continue
        rmax = max(_radius_of(size_by, n.id, inc, weight_of) for n in lst)
        spacing[yi] = max(int(row_gap), 2 * rmax + 18)
        height = max(height, spacing[yi] * (len(lst) - 1) + 2 * rmax + 140)
    max_yi = max(picked)
    pos: dict[str, dict] = {}
    for yi, lst in picked.items():
        if not lst:
            continue
        sp = spacing[yi]
        n = len(lst)
        y0 = (height - sp * (n - 1)) / 2
        x = 90 + yi * col_gap
        for i, nd in enumerate(lst):
            pos[nd.id] = {"x": int(x), "y": int(y0 + i * sp), "layer": yi,
                          "r": _radius_of(size_by, nd.id, inc, weight_of),
                          "in_deg": inc.get(nd.id, 0),
                          "stag": 18 if i % 2 else 0}
    labelled: list[dict] = []
    for yi in sorted(picked):
        yr = col_ids[yi]
        has_year = any(int(years.get(n.id) or 0) for n in picked[yi])
        labelled.append({"x": int(90 + yi * col_gap),
                         "label": ("未知" if yr == unknown_year and not has_year else str(yr))})
    return {"pos": pos, "layers": len(col_ids),
            "edges": _edge_rows(pos, edges, len(col_ids), drop_long=False),
            "width": int(90 + max_yi * col_gap + 150), "height": int(height),
            "bands": _bands(picked, pos, groups), "columns": labelled}


def label_for(title: str, ident: str, *, max_len: int = 18) -> str:
    """呈现参数，不是页面私货：截断长度由消费配置给（判据 G4），全称留给 hover 卡。"""
    text = (title or ident).strip()
    if max_len > 3 and len(text) > max_len:
        return text[:max_len - 1] + "…"
    return text


def short_id(ident: str, *, max_len: int = 12) -> str:
    """简版标签：只显标识（拥挤时的退路），全称仍在 hover 卡。"""
    text = (ident or "").strip()
    return text if len(text) <= max_len else text[:max_len]
