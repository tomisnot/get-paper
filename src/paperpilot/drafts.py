"""草稿：校验 + 体检。

两条规矩（docs/设计-总纲.md §5）：
- **条目只能引用库里存在的论文**，写不进去就是写不进去，不静默丢弃；
- 体检只报不拦：`no_card` / `recently_shown` / `top_share`。
"""

from __future__ import annotations

from . import db
from .ids import base_id
from .repo import Repo

KINDS = ("linear", "network")


class DraftError(ValueError):
    """草稿内容不合法。消息本身就是给 AI 的改法提示。"""


def normalize(kind: str, payload: dict, repo: Repo) -> dict:
    if kind not in KINDS:
        raise DraftError(f"kind 只能是 linear / network，收到 {kind!r}")
    if kind == "linear":
        return _normalize_linear(payload, repo)
    return _normalize_network(payload, repo)


def _normalize_linear(payload: dict, repo: Repo) -> dict:
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise DraftError("linear 草稿必须有非空的 entries 列表")
    seen: set[str] = set()
    out: list[dict] = []
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise DraftError(f"entries[{index}] 必须是对象")
        aid = base_id(str(raw.get("arxiv_id") or ""))
        if not aid:
            raise DraftError(f"entries[{index}] 缺 arxiv_id")
        if aid in seen:
            raise DraftError(f"{aid} 在 entries 里出现了两次")
        if repo.paper(aid) is None:
            raise DraftError(f"{aid} 不在库里：先 fetch_paper 或 fetch_papers")
        why = str(raw.get("why") or "").strip()
        if not why:
            raise DraftError(f"{aid} 缺 why（一句为什么）——写不出理由就说明它不该进稿")
        seen.add(aid)
        entry: dict = {"arxiv_id": aid, "why": why}
        group = str(raw.get("group") or "").strip()
        if group:
            entry["group"] = group
        out.append(entry)
    return {"entries": out}


def _normalize_network(payload: dict, repo: Repo) -> dict:
    """网络草稿 = **自由画布**：不是只有论文，也不是只有引文。

    节点可以是论文（带 arxiv_id，点开进详情页），也可以是文字框、便签、分组框——
    AI 调研完想画什么就画什么。三条仍然硬性的规矩：
    1. `id` 必须有且唯一（边按它引）；
    2. 论文节点**必须在库里**（写不进去就是写不进去，不静默丢弃）；
    3. 边的两端、`parent` 指的框，都必须真的存在（悬空引用是错，不做"尽力而为"）。
    `kind` 只决定缺省样式，不是白名单；形状可随 `shape` 直接给。
    """
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise DraftError("network 草稿必须有非空的 nodes 列表")
    clean_nodes: list[dict] = []
    present: set[str] = set()
    for index, raw in enumerate(nodes):
        if not isinstance(raw, dict):
            raise DraftError(f"nodes[{index}] 必须是对象")
        aid = base_id(str(raw.get("arxiv_id") or ""))
        nid = node_id(raw, index)
        if nid in present:
            raise DraftError(f"{nid} 在 nodes 里出现了两次")
        kind = str(raw.get("kind") or ("paper" if aid else "text")).strip().lower()
        # 论文节点的 label 可以省：**代码从库里补标题**，AI 不必手抄（省 token 也省出错）
        label = str(raw.get("label") or "")[:80]
        if kind == "paper":
            if not aid:
                raise DraftError(f"nodes[{index}] 是 paper 但没给 arxiv_id")
            paper = repo.paper(aid)
            if paper is None:
                raise DraftError(f"{aid} 不在库里：先把它拉进库再画网络")
            label = label or str(paper.get("title") or aid)[:80]
        elif not label:
            raise DraftError(f"nodes[{index}]（{kind}）缺 label：文字元素得先有字")
        present.add(nid)
        # **只存有值的字段**：空串/0/None 一律不落盘。实测每个节点原来 235 字节里
        # 一半是 "" 和 null，回读时这些空字段纯属噪音（60 个点的草稿白吃 7KB）。
        node: dict = {"id": nid, "kind": kind}
        if label:
            node["label"] = label
        if aid:
            node["arxiv_id"] = aid
        for key in ("parent", "shape", "fill", "color"):
            value = str(raw.get(key) or "").strip()
            if value:
                node[key] = value
        for key in ("width", "height"):
            value = float(raw.get(key) or 0) or 0.0
            if value:
                node[key] = value
        if raw.get("rank") is not None and str(raw.get("rank")) != "":
            node["rank"] = raw.get("rank")
        clean_nodes.append(node)
    for node in clean_nodes:                      # parent 必须指向真实存在的框
        parent = node.get("parent", "")
        if parent and (parent not in present or parent == node["id"]):
            raise DraftError(f"{node['id']} 的 parent={parent!r} 不存在："
                             f"先加一个 kind=group 的节点当框")
    edges = payload.get("edges") or []
    if not isinstance(edges, list):
        raise DraftError("edges 必须是列表")
    clean_edges: list[dict] = []
    for index, raw in enumerate(edges):
        if not isinstance(raw, dict):
            raise DraftError(f"edges[{index}] 必须是对象")
        src, dst = str(raw.get("src") or "").strip(), str(raw.get("dst") or "").strip()
        if not src or not dst:
            raise DraftError(f"edges[{index}] 缺 src 或 dst")
        if src not in present or dst not in present:
            raise DraftError(f"edges[{index}] 的端点没在 nodes 里：{src} -> {dst}")
        edge: dict = {"src": src, "dst": dst}
        label = str(raw.get("label") or raw.get("note") or "")[:120]
        if label:
            edge["label"] = label
        style = str(raw.get("style") or "").strip()
        if style and style != "solid":
            edge["style"] = style
        arrow = str(raw.get("arrow") or "").strip()
        if arrow and arrow != "normal":
            edge["arrow"] = arrow
        clean_edges.append(edge)
    layout = payload.get("layout") or {}
    if not isinstance(layout, dict):
        raise DraftError("layout 必须是对象")
    rankdir = str(layout.get("rankdir") or "").strip()
    return {"nodes": clean_nodes, "edges": clean_edges,
            "layout": {"rankdir": "LR" if rankdir == "LR" else "TB"}}


def node_id(raw: dict, index: int = -1) -> str:
    """节点的**稳定主键**：显式 id > arxiv_id > 序号。

    合并不是按数组下标认人（下一批的顺序会变），所以这套推导必须是唯一一份——
    归一化与增量合并都调它，否则"合并"会变成"重复添加"。
    """
    aid = str(raw.get("arxiv_id") or "").strip()
    return str(raw.get("id") or aid or (f"n{index + 1}" if index >= 0 else "")).strip()


def apply_merge(kind: str, existing: dict | None, incoming: dict,
                remove: list[str] | None = None) -> dict:
    """把一批**增量**并进现有草稿，返回待校验的整份载荷。

    合并按主键，不按下标：没提到的**不动**，要删必须显式列进 ``remove``
    （"这次没写" ≠ "删掉"，否则分批写会误删前一批）。
    linear 按 ``arxiv_id``（重复=就地更新，新条目追加到末尾 ⇒ 数组顺序仍是页面顺序）；
    network 节点按 ``id``、边按 ``(src, dst)``；``layout`` 浅合并。
    """
    base = existing or {}
    drop = {str(x).strip() for x in (remove or []) if str(x).strip()}
    if kind == "linear":
        entries = [dict(e) for e in base.get("entries") or []]
        pos = {e["arxiv_id"]: i for i, e in enumerate(entries)}
        for raw in incoming.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            aid = base_id(str(raw.get("arxiv_id") or ""))
            if aid and aid in pos:
                entries[pos[aid]] = {**entries[pos[aid]], **raw, "arxiv_id": aid}
            elif aid:
                pos[aid] = len(entries)
                entries.append({**raw, "arxiv_id": aid})
        return {"entries": [e for e in entries if e["arxiv_id"] not in drop]}

    nodes = [dict(n) for n in base.get("nodes") or []]
    order = {n["id"]: i for i, n in enumerate(nodes)}
    for index, raw in enumerate(incoming.get("nodes") or []):
        if not isinstance(raw, dict):
            continue
        nid = node_id(raw, index)
        if nid and nid in order:
            nodes[order[nid]] = {**nodes[order[nid]], **raw, "id": nid}
        elif nid:
            order[nid] = len(nodes)
            nodes.append({**raw, "id": nid})
    kept = [n for n in nodes if n["id"] not in drop]
    keys = {(e["src"], e["dst"]): i for i, e in enumerate(base.get("edges") or [])}
    edges = [dict(e) for e in base.get("edges") or []]
    for raw in incoming.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        key = (str(raw.get("src") or "").strip(), str(raw.get("dst") or "").strip())
        if not all(key):
            continue
        if key in keys and keys[key] < len(edges):
            edges[keys[key]] = {**edges[keys[key]], **raw}
        else:
            keys[key] = len(edges)
            edges.append(dict(raw))
    alive = {n["id"] for n in kept}
    edges = [e for e in edges if e["src"] in alive and e["dst"] in alive]
    layout = {**(base.get("layout") or {}), **(incoming.get("layout") or {})}
    return {"nodes": kept, "edges": edges, "layout": layout}


def entry_ids(kind: str, payload: dict) -> list[str]:
    """稿子里出现的**论文**（文字框/便签没有 arxiv，不参与"在不在库/有没有卡"）。"""
    if kind == "linear":
        return [e["arxiv_id"] for e in payload.get("entries") or []]
    return [n["arxiv_id"] for n in payload.get("nodes") or [] if n.get("arxiv_id")]


def checkup(repo: Repo, kind: str, payload: dict, *, recent_days: float = 7.0,
            exclude_draft_id: int | None = None) -> dict:
    """体检：没卡的、近期已出现过的、单一分类最高占比，**以及画布是不是大得没人看**。

    ``too_big`` 是**提示不是门**：读不动的是人的眼睛，不是布局引擎——dot 几百个点
    照样画得出来。真太大了就给一句改法（拆成几张草稿），但不拦。
    """
    ids = entry_ids(kind, payload)
    elements = (len(payload.get("entries") or []) if kind == "linear"
                else len(payload.get("nodes") or []))
    edges_n = len(payload.get("edges") or [])
    limit = 60 if kind == "linear" else 40
    too_big = ""
    if elements > limit or (kind == "network" and edges_n > 80):
        too_big = (f"{elements} 个元素 / {edges_n} 条连线，人读不动了："
                   f"拆成两张草稿，名字就是情景")
    if not ids:
        return {"total": 0, "no_card": [], "recently_shown": [],
                "top_share": 0.0, "top_category": "", "elements": elements,
                "edges": edges_n, "too_big": too_big}
    cards = repo.cards_of(ids)
    no_card = [aid for aid in ids if not (cards.get(aid) or {}).get("tldr")]

    recent: set[str] = set()
    cutoff = db.days_ago_iso(recent_days)
    for draft in repo.drafts():
        if draft["id"] == exclude_draft_id or draft["updated_at"] < cutoff:
            continue
        recent.update(entry_ids(draft["kind"], draft["payload"]))
    recently_shown = [aid for aid in ids if aid in recent]

    papers = repo.papers_by_ids(ids)
    counts: dict[str, int] = {}
    for aid in ids:
        category = (papers.get(aid) or {}).get("primary_category") or ""
        counts[category] = counts.get(category, 0) + 1
    top_category, top_count = max(counts.items(), key=lambda kv: kv[1]) if counts else ("", 0)
    return {
        "total": len(ids),
        "no_card": no_card,
        "recently_shown": recently_shown,
        "top_category": top_category,
        "top_share": round(top_count / len(ids), 3),
        "elements": elements, "edges": edges_n, "too_big": too_big,
    }


def describe(draft: dict) -> dict:
    return {"id": draft["id"], "kind": draft["kind"], "title": draft["title"],
            "note": draft["note"], "count": len(entry_ids(draft["kind"], draft["payload"])),
            "created_at": draft["created_at"], "updated_at": draft["updated_at"]}
