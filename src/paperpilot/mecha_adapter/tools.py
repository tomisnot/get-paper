"""AI 能力面：读工具直接定义，写工具由命令投影。

**不手抄第二份清单**：写工具的 name/parameters/output 全从 `CommandSpec` 投影，
声明的 scope 若没授予，`project()` 会直接不投影（看不见，省得 AI 去试）。
"""

from __future__ import annotations

import random

from mecha.errors import MechaError
from mecha.projection import project
from mecha.tools import ToolRegistry, define_tool

from .. import drafts as draft_lib
from .. import scoring
from ..context import Context
from . import config as config_mod
from .args import with_defaults
from .limits import KEEP, cap, with_cap
from .params import describe

INSTRUCTIONS = (
    "PaperPilot 论文台账。代码只记账（论文、兴趣画像、扫描账），结果由你写成草稿。\n"
    "\n"
    "## 怎么干\n"
    "1. 人说要什么 → 挑能力：fetch_papers（分类×日历全量扫）/ fetch_query（短语扫）/ "
    "sample（抽样，可 far 打破茧房）/ score_papers（按画像排序）。\n"
    "2. 进稿的每篇先 read_paper 读摘要，再 write_card 写卡——卡是「读过」的物证。\n"
    "3. write_linear / write_network 落成一份草稿：linear=条目+一句 why；"
    "network=节点+边（论文/文字/便签/框混排，坐标由 Graphviz 算）。"
    "**两者都默认增量**（mode=merge）：只发这一批，按 arxiv_id / 节点 id 认人；"
    "要删必须写进 remove（没提到≠删掉）。整份重来才用 mode=replace。"
    "回执里的 checkup 会告诉你哪些还没卡、哪些近期出现过。\n"
    "4. 草稿名就是情景（「1024」「随手十篇」），人靠名字回想当时要什么。\n"
    "\n"
    "## 纪律\n"
    "- 进稿必须有卡；同一篇别连着端；每条线性条目都要写得出为什么。\n"
    "- 没有「日报/推荐流」这种预设流程——想要什么就说。\n"
    "## 常见下一步\n"
    "- 库里没货：fetch_papers 或 fetch_query；换口味：sample(mode='far')。\n"
    "- 想追脉络：sync_citations / sync_cited_by 之后 read_references / related_papers / "
    "upstream_clusters。\n"
    "- 工具报 unknown_key / bad_config_value：先 read_config 看可写键。"
)

_OBJ = {"type": "object"}


def _window_bounds(text: str) -> tuple[str, str]:
    """把 `from~to` 解析成两个日期串（`score_papers` 的窗口参数用）。"""
    first = (text or "").replace("，", ",").split(",")[0].strip()
    if "~" not in first:
        raise MechaError(f"窗口 {first!r} 缺 `~`", kind="bad_window",
                         hint="格式：2026-01-01~2026-01-31")
    lo, hi = (part.strip() for part in first.split("~", 1))
    if not lo or not hi:
        raise MechaError(f"窗口 {first!r} 两端都要写", kind="bad_window",
                         hint="格式：2026-01-01~2026-01-31")
    return lo, hi


def build_tools(ctx: Context, software, *, policy=None, side: str = "ai") -> ToolRegistry:
    repo, settings = ctx.repo, ctx.settings
    registry = ToolRegistry()

    def read_tool(*, name: str, description: str, parameters: dict, required: list[str],
                  execute) -> None:
        registry.register(define_tool(name=name, description=description,
                                      parameters=describe(parameters, owner=name),
                                      output_schema={"type": "object", "required": required},
                                      # 省略的可选参数会以 None 到达（MCP 签名是合成的）；
                                      # 长列表过体积闸，截断时说清截了多少、去哪看全量。
                                      execute=with_cap(with_defaults(execute),
                                                       where=f"{name} 的结果")))

    def _require_paper(arxiv_id: str) -> dict:
        view = repo.paper_view(arxiv_id)
        if view is None:
            raise MechaError(f"库里没有 {arxiv_id}", kind="not_found",
                             hint="先 fetch_paper(arxiv_id=…)")
        return view

    # ------------------------------------------------------------------ 读
    def _query(limit: int = 20, offset: int = 0) -> dict:
        # 页大小就锁在体积闸的 KEEP 上：这样 `next_offset` 与真正返回的条数**永远一致**，
        # 不会出现"我回了 20 条却让你从 100 续读"的错位。
        limit = max(1, min(KEEP, int(limit)))
        offset = max(0, int(offset))
        rows = repo.list_papers(limit=limit, offset=offset)
        total = repo.count_papers()
        out = {"total": total, "offset": offset, "papers": [_brief(p) for p in rows]}
        if offset + len(rows) < total:
            out["next_offset"] = offset + len(rows)
            out["where"] = f"还有 {total - offset - len(rows)} 篇没回；再调 offset=… 续读"
        return out

    read_tool(
        name="query_papers", description="按发布日期倒序列出库内论文（分页：每页最多 20，续读用 next_offset）。",
        parameters={"limit": {"type": "integer", "description": "每页条数（默认 20，上限 20）"},
                    "offset": {"type": "integer", "description": "从第几条开始（续读用上次的 next_offset）"}},
        required=["papers", "total"], execute=_query)

    def _search(query: str = "", limit: int = 30) -> dict:
        papers = repo.search(query, limit=int(limit))
        return {"count": len(papers), "papers": [_brief(p) for p in papers]}

    read_tool(
        name="search_papers", description="库内检索（标题 / 摘要 / 编号）。",
        parameters={"query": {"type": "string"}, "limit": {"type": "integer"}},
        required=["papers", "count"], execute=_search)

    def _read_paper(arxiv_id: str) -> dict:
        view = _require_paper(arxiv_id)
        view["notes"] = repo.notes(view["arxiv_id"])
        return {"paper": view}

    read_tool(
        name="read_paper", description="一篇论文的全部状态：元数据、卡、人的状态、笔记、进过哪些草稿。",
        parameters={"arxiv_id": {"type": "string"}}, required=["paper"],
        execute=_read_paper)

    read_tool(
        name="read_profile", description="读兴趣画像：三维 top、衰减后读数、熵、锚点、信号计数。",
        parameters={"top": {"type": "integer"}}, required=["profile"],
        execute=lambda top=12: {"profile": scoring.profile_view(repo, settings,
                                                                top=int(top))})

    read_tool(
        name="query_drafts", description="列出草稿（可按 kind 过滤），带每页当前的默认草稿。",
        parameters={"kind": {"type": "string"}}, required=["drafts", "count"],
        execute=lambda kind="": {
            "count": len(repo.drafts(kind=kind)),
            "drafts": [draft_lib.describe(d) for d in repo.drafts(kind=kind)],
            "defaults": {"linear": repo.setting("page.linear.default", ""),
                         "network": repo.setting("page.network.default", "")}})

    def _read_draft(ident: str = "") -> dict:
        draft = repo.draft(ident.strip() or None)
        if draft is None:
            raise MechaError(f"没有草稿 {ident!r}", kind="not_found",
                             hint="先 query_drafts 看有哪些")
        ids = draft_lib.entry_ids(draft["kind"], draft["payload"])
        papers = repo.papers_by_ids(ids)
        cards = repo.cards_of(ids)
        stars = repo.star_map(ids)
        entries = []
        for aid in ids:
            brief = _brief(papers.get(aid) or {"arxiv_id": aid})
            brief["card"] = cards.get(aid)
            brief["starred"] = bool(stars.get(aid))
            entries.append(brief)
        return {"draft": {"id": draft["id"], "kind": draft["kind"], "title": draft["title"],
                          "note": draft["note"], "created_at": draft["created_at"],
                          "updated_at": draft["updated_at"]},
                "entries": entries}

    read_tool(
        name="read_draft", description="读一份草稿（省略 ident 取最近修改的），带论文卡与收藏态。",
        parameters={"ident": {"type": "string"}}, required=["draft", "entries"],
        execute=_read_draft)

    #: 窗口内最多评多少篇。**与 limit 无关**：候选面由时间窗决定，不由"要几条"决定。
    #: 旧写法用 `limit*4` 取最新的一小撮 ⇒ `days=6, limit=10` 只看 40 篇、
    #: 全是最后一天的，窗口里更早的好论文根本没进候选（静默截断，实测踩到）。
    _SCORE_POOL_CAP = 2000

    def _score(arxiv_ids: str = "", days: int = 7, limit: int = 50,
               windows: str = "", offset: int = 0) -> dict:
        if arxiv_ids.strip():
            ids = [part.strip() for part in arxiv_ids.replace("，", ",").split(",")
                   if part.strip()]
            papers = list(repo.papers_by_ids(ids).values())
        elif (windows or "").strip():
            lo, hi = _window_bounds(windows)
            papers = repo.between(lo, hi, limit=_SCORE_POOL_CAP)
        else:
            papers = repo.recent(days=max(1, int(days)), limit=_SCORE_POOL_CAP)
        ranked = scoring.rank(repo, papers, settings.half_life_days)
        start = max(0, int(offset))
        page = ranked[start:start + int(limit)]
        return {"count": len(page), "pool": len(papers), "offset": start,
                "results": [{"arxiv_id": item["paper"]["arxiv_id"],
                             "title": item["paper"]["title"],
                             "published": (item["paper"].get("published_at") or "")[:10],
                             "score": item["score"], "why": item["why"]} for item in page]}

    read_tool(
        name="score_papers",
        description="按画像给一个时间窗内的**全部**论文打分并排序（0~1 + 为什么）；"
                    "候选面由 windows/days 决定，不由 limit 决定。分页用 offset。",
        parameters={"arxiv_ids": {"type": "string", "description": "逗号分隔（给了就只评这些）"},
                    "days": {"type": "integer", "description": "回溯天数（windows 省略时生效）"},
                    "windows": {"type": "string",
                                "description": "日期段 from~to，如 2026-01-01~2026-01-31"},
                    "limit": {"type": "integer", "description": "本页多少条（默认 50，上限 20）"},
                    "offset": {"type": "integer", "description": "从第几条开始（续读用）"}},
        required=["count", "results"], execute=_score)

    def _sample(n: int = 10, days: int = 365, mode: str = "random",
                exclude_recent_drafts: bool = True) -> dict:
        papers = repo.recent(days=max(1, int(days)), limit=5000)
        if not papers:
            raise MechaError(f"近 {days} 天库里没有论文", kind="empty_library",
                             hint="先 fetch_papers，或把 days 放大")
        if exclude_recent_drafts:
            seen = repo.recent_draft_ids(days=settings.recent_draft_days)
            papers = [p for p in papers if p["arxiv_id"] not in seen]
        snapshot = scoring.profile_snapshot(repo, settings.half_life_days)
        top = set(sorted(((k, v["w"]) for k, v in snapshot["category"].items() if v["w"] > 0),
                         key=lambda kv: -kv[1])[:max(3, settings.default_regions_n)])
        if mode == "recent":
            picked = papers[:int(n)]
        elif mode == "focused":
            pool = [p for p in papers if p["primary_category"] in top]
            picked = random.sample(pool, min(int(n), len(pool)))
        elif mode == "far":
            pool = [p for p in papers if p["primary_category"] not in top]
            picked = random.sample(pool, min(int(n), len(pool)))
        else:
            picked = random.sample(papers, min(int(n), len(papers)))
        return {"mode": mode, "count": len(picked), "pool": len(papers),
                "papers": [_brief(p) for p in picked]}

    read_tool(
        name="sample",
        description="抽样（不是排序）：random / recent / focused（画像分类内）/ far（画像分类外）。",
        parameters={"n": {"type": "integer"}, "days": {"type": "integer"},
                    "mode": {"type": "string"}, "exclude_recent_drafts": {"type": "boolean"}},
        required=["count", "papers"], execute=_sample)

    read_tool(
        name="scanned_days", description="已经扫过的自然日清单（扫描账）。",
        parameters={}, required=["days", "count"],
        execute=lambda: {"days": sorted(repo.scanned_days()),
                         "count": len(repo.scanned_days())})

    read_tool(
        name="read_references", description="这一篇引了谁（按被引数降序，标出哪些在库里）。",
        parameters={"arxiv_id": {"type": "string"}}, required=["references", "count"],
        execute=lambda arxiv_id: {"count": len(repo.edges_from(arxiv_id)),
                                  "references": repo.edges_from(arxiv_id)})

    read_tool(
        name="read_citations", description="谁引用了这一篇（按被引数降序）。",
        parameters={"arxiv_id": {"type": "string"}}, required=["citations", "count"],
        execute=lambda arxiv_id: {"count": len(repo.edges_to(arxiv_id)),
                                  "citations": repo.edges_to(arxiv_id)})

    read_tool(
        name="related_papers", description="共引相似：引用集交集最大的库内论文。",
        parameters={"arxiv_id": {"type": "string"}, "limit": {"type": "integer"}},
        required=["related", "count"],
        execute=lambda arxiv_id, limit=10: {
            "count": len(repo.related(arxiv_id, limit=int(limit))),
            "related": repo.related(arxiv_id, limit=int(limit))})

    read_tool(
        name="upstream_clusters",
        description="关键上游簇：库里多篇反复引的同一文献——领域思想源头。",
        parameters={"min_count": {"type": "integer"}, "limit": {"type": "integer"}},
        required=["clusters", "count"],
        execute=lambda min_count=2, limit=20: {
            "count": len(repo.upstream_clusters(min_count=int(min_count), limit=int(limit))),
            "clusters": repo.upstream_clusters(min_count=int(min_count), limit=int(limit))})

    read_tool(
        name="read_activity", description="读事件流（仪表盘同源，可按 op 前缀与操作者过滤）。",
        parameters={"limit": {"type": "integer"}, "op": {"type": "string"},
                    "actor": {"type": "string"}},
        required=["events", "count"],
        execute=lambda limit=50, op="", actor="": {
            "count": len(repo.events(limit=int(limit), op_prefix=op, actor=actor)),
            "events": repo.events(limit=int(limit), op_prefix=op, actor=actor)})

    read_tool(
        name="read_config", description="读当前生效的标量配置、可写键与信号权重。",
        parameters={}, required=["config", "writable"],
        execute=lambda: {"config": {key: getattr(settings, key)
                                    for key in config_mod.CONFIG_KEYS},
                         "writable": {key: {"type": spec.kind, "lo": spec.lo, "hi": spec.hi}
                                      for key, spec in config_mod.CONFIG_SCHEMA.items()},
                         "signal_weights": dict(settings.signal_weights)})

    read_tool(
        name="read_status", description="库规模、卡数、收藏数、草稿数、事件数、扫过天数、引文边。",
        parameters={}, required=["papers", "cards", "drafts"],
        execute=lambda: _status(repo))

    # ------------------------------------------------------------------ 写（投影）
    if policy is not None:
        software.commands.bind_scope_policy(policy)
    for spec in software.commands.specs():
        tool = project(spec, execute=_bridge(software, spec.name), policy=policy, side=side)
        if tool is not None:
            registry.register(tool)
    return registry


def _bridge(software, name: str):
    """命令 → 工具的执行桥：回执解包成 `value`，失败重新抛成 MechaError。

    刻意**不让工具层再判一次写权**：门在命令面，这里只转发。
    """
    def execute(**kwargs):
        from mecha.surface import ExecutionContext

        result = software.commands.invoke(
            name, kwargs, context=ExecutionContext(), gate=software.gate,
            channel=software.channels["ai"], approval=software.approval)
        if result.get("is_error"):
            error = result.get("error") or {}
            info = error.get("info") or {}
            raise MechaError(str(error.get("message") or f"命令 {name} 失败"),
                             kind=str(info.get("kind") or "command_failed"),
                             hint=str(info.get("hint") or ""),
                             suggest=str(info.get("suggest") or ""))
        return cap(dict(result.get("value") or {}), where=f"{name} 的结果")

    execute.__name__ = name
    return execute


def _brief(paper: dict) -> dict:
    return {"arxiv_id": paper.get("arxiv_id", ""), "title": paper.get("title", ""),
            "published": (paper.get("published_at") or "")[:10],
            "primary_category": paper.get("primary_category", ""),
            "authors": (paper.get("authors") or [])[:4]}


def _status(repo) -> dict:
    return {"papers": repo.count_papers(),
            "cards": repo.conn.execute("SELECT COUNT(*) c FROM cards").fetchone()["c"],
            "starred": len(repo.starred_ids()),
            "drafts": repo.conn.execute("SELECT COUNT(*) c FROM drafts").fetchone()["c"],
            "events": repo.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
            "scanned_days": len(repo.scanned_days()),
            "edges": repo.edge_counts()}
