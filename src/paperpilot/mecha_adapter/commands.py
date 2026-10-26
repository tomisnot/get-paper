"""有副作用的动作：每条一个 `CommandSpec`，经 CommandRegistry 统一执行。

为什么写在这里而不是工具里：命令是**受治理的动作**（写权、作用域、参数校验、
operation_id、审计），工具只是它的投影。写权检查发生在任何副作用之前，
所以域写（SQLite）根本不会在无权时发生。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from mecha.commands import CommandResult, define_command
from mecha.errors import MechaError
from mecha.operations import current_operation

from .. import drafts as draft_lib
from .. import scoring
from ..arxiv_client import ArxivError
from ..context import Context
from ..ids import base_id
from . import config as config_mod
from .args import command_handler as _handler_adapter
from .params import describe


def _actor() -> str:
    """这次调用的操作者（来自框架的操作范围，不靠参数、不靠猜）。"""
    op = current_operation()
    return op.actor if op is not None and op.actor else "ai"


def _ok(**values) -> CommandResult:
    return CommandResult(ok=True, values=values)


def _out(props: dict, required: list[str]) -> dict:
    """回执 schema：只声明形状，不做参数描述兜底。"""
    return {"type": "object", "properties": props, "required": list(required)}


def _schema(props: dict, required: list[str], *, owner: str = "") -> dict:
    """入参 schema：参数描述兜底（手写优先，没写从常用表补，补不出来构建期就炸）。"""
    return {"type": "object", "properties": describe(props, owner=owner),
            "required": list(required)}


_ID = {"type": "string", "description": "arXiv 编号，如 2610.25020"}
_DAYS = {"type": "integer", "description": "回溯天数"}
_WINDOWS = {"type": "string", "description": "日期段，如 2026-03-01~2026-03-07"}


def _network_errors(fn):
    """把"外部服务连不上"翻译成可教学错误。

    否则 `ArxivError` 会穿透到工具层被归一成 `internal_error`——模型看到"内部错误"
    只会重试，而真正该说的是"arXiv 连不上，稍后再试或先用库里的数据"。
    """

    def wrapper(**kwargs):
        try:
            return fn(**kwargs)
        except ArxivError as exc:
            raise MechaError(f"arXiv 连不上：{exc}", kind="arxiv_unavailable",
                             hint="网络或限速问题：过一会儿重试；"
                                  "也可以先用库里已经抓到的论文出稿") from exc

    wrapper.__name__ = getattr(fn, "__name__", "command")
    return wrapper


def build_commands(ctx: Context, software) -> list[str]:
    repo, settings = ctx.repo, ctx.settings
    names: list[str] = []

    def register(spec, handler) -> None:
        software.commands.register(spec, _network_errors(_handler_adapter(handler)))
        names.append(spec.name)

    # ------------------------------------------------------------------ 抓取
    def _window(days: int, windows: str) -> tuple[datetime, datetime]:
        text = (windows or "").strip()
        if not text:
            until = datetime.utcnow()
            return until - timedelta(days=max(1, int(days))), until
        lo = hi = None
        for segment in text.replace("，", ",").split(","):
            segment = segment.strip()
            if not segment:
                continue
            if "~" not in segment:
                raise MechaError(f"窗口 {segment!r} 缺 `~`", kind="bad_window",
                                 hint="格式：2026-03-01~2026-03-07（to 含当天整天）")
            left, right = (part.strip() for part in segment.split("~", 1))
            try:
                start = datetime.fromisoformat(left) if left else datetime.utcnow()
                end = datetime.fromisoformat(right) if right else datetime.utcnow()
            except ValueError as exc:
                raise MechaError(f"窗口 {segment!r} 不是合法日期", kind="bad_window",
                                 hint="用 YYYY-MM-DD") from exc
            if end.hour == 0 and end.minute == 0:
                end = end + timedelta(days=1) - timedelta(microseconds=1)
            lo = start if lo is None else min(lo, start)
            hi = end if hi is None else max(hi, end)
        return lo, hi

    def _regions(explicit: str) -> list[str]:
        if explicit.strip():
            return [part.strip() for part in explicit.replace("，", ",").split(",")
                    if part.strip()]
        snapshot = scoring.profile_snapshot(repo, settings.half_life_days)
        ranked = sorted(((k, v["w"]) for k, v in snapshot["category"].items() if v["w"] > 0),
                        key=lambda kv: -kv[1])
        return [k for k, _ in ranked[:max(1, settings.default_regions_n)]]

    def fetch_papers(days: int = 3, windows: str = "", regions: str = "") -> CommandResult:
        since, until = _window(days, windows)
        group = _regions(regions)
        if not group:
            raise MechaError("画像里没有正权重分类，也没有显式给 regions",
                             kind="empty_regions",
                             hint="传 regions=quant-ph,physics.atom-ph，或先 record_signal 喂兴趣")
        client = ctx.client()
        try:
            papers, stats = client.fetch_regions(
                group, since, until, max_pages_per_day=settings.max_pages_per_day,
                max_requests=settings.max_requests)
        finally:
            client.close()
        found_by = f"region:{','.join(group)}@{since.date().isoformat()}"
        result = repo.upsert_papers(papers, found_by=found_by)
        repo.log_scan(actor=_actor(), payload={
            "source": "region", "regions": group, "days": stats["days"],
            "requests": stats["requests"], "hits": len(papers), "new": result["new"]})
        return _ok(new=result["new"], updated=result["updated"], regions=group,
                   range=f"{since.date()}~{until.date()}", stats=stats)

    register(define_command(
        name="fetch_papers", scope=("fetch",), side_effect=True,
        description="按分类 × 日历扫 arXiv 并落库；regions 省略时用画像分类 top-N。",
        parameters=_schema({"days": _DAYS, "windows": _WINDOWS,
                            "regions": {"type": "string",
                                        "description": "分类，逗号分隔；省略=画像 top-N"}},
                           ["days"]),
        output_schema=_out({"new": {"type": "integer"},
                               "updated": {"type": "integer"},
                               "regions": {"type": "array"},
                               "range": {"type": "string"},
                               "stats": {"type": "object"}},
                              ["new", "updated", "regions", "range", "stats"]),
    ), fetch_papers)

    def fetch_query(queries: str, days: int = 30, windows: str = "",
                    limit: int = 200) -> CommandResult:
        phrases = [part.strip().strip('"')
                   for part in (queries or "").replace("，", ",").split(",") if part.strip()]
        if not phrases:
            raise MechaError("queries 不能为空", kind="bad_params",
                             hint="例：queries=neutral atom,optical tweezer")
        since, until = _window(days, windows)
        client = ctx.client()
        try:
            papers, stats = client.fetch_phrases(phrases, since, until, limit=int(limit))
        finally:
            client.close()
        result = repo.upsert_papers(
            papers, found_by=f'query:"{phrases[0]}"@{since.date().isoformat()}')
        repo.log_scan(actor=_actor(), payload={
            "source": "query", "queries": phrases, "requests": stats["requests"],
            "hits": len(papers), "new": result["new"],
            "range": f"{since.date()}~{until.date()}"})
        return _ok(new=result["new"], updated=result["updated"], queries=phrases,
                   range=f"{since.date()}~{until.date()}", stats=stats)

    register(define_command(
        name="fetch_query", scope=("fetch",), side_effect=True,
        description="按检索式短语扫 arXiv 并落库（短语之间是 OR）。",
        parameters=_schema({"queries": {"type": "string", "description": "短语，逗号分隔"},
                            "days": _DAYS, "windows": _WINDOWS,
                            "limit": {"type": "integer", "description": "最多落多少篇"}},
                           ["queries"]),
        output_schema=_out({"new": {"type": "integer"}, "updated": {"type": "integer"},
                               "queries": {"type": "array"}, "range": {"type": "string"},
                               "stats": {"type": "object"}},
                              ["new", "updated", "queries", "range", "stats"]),
    ), fetch_query)

    def fetch_paper(arxiv_id: str) -> CommandResult:
        aid = base_id(arxiv_id)
        if not aid:
            raise MechaError("arxiv_id 为空", kind="bad_params", hint="形如 2610.25020")
        client = ctx.client()
        try:
            paper = client.get(aid)
        finally:
            client.close()
        if paper is None:
            raise MechaError(f"arXiv 上找不到 {aid}", kind="not_found",
                             hint="检查编号；带版本号也可以")
        result = repo.upsert_papers([paper], found_by=f"id:{aid}")
        return _ok(arxiv_id=aid, new=result["new"], title=paper["title"],
                       hint="下一步 read_paper 看摘要，再 write_card 建卡")

    register(define_command(
        name="fetch_paper", scope=("fetch",), side_effect=True,
        description="按 arXiv 编号把单篇拉进库（幂等）。",
        parameters=_schema({"arxiv_id": _ID}, ["arxiv_id"]),
        output_schema=_out({"arxiv_id": {"type": "string"}, "new": {"type": "integer"},
                               "title": {"type": "string"}},
                              ["arxiv_id", "new", "title"]),
    ), fetch_paper)

    # ------------------------------------------------------------------ 读过的凭证
    def write_card(arxiv_id: str, tldr: str = "", problem: str = "", method: str = "",
                   results: str = "", novelty: str = "", terms: str = "") -> CommandResult:
        aid = base_id(arxiv_id)
        term_list = [t.strip() for t in (terms or "").replace("，", ",").split(",")
                     if t.strip()]
        if not repo.write_card(aid, {"tldr": tldr, "problem": problem, "method": method,
                                     "results": results, "novelty": novelty,
                                     "terms": term_list}, model="dsh"):
            raise MechaError(f"库里没有 {aid}", kind="not_found",
                             hint="先 fetch_paper 再写卡")
        repo.record(op="write_card", target=aid, actor=_actor(),
                    after={"has_tldr": bool(tldr), "terms": len(term_list)})
        return _ok(arxiv_id=aid, terms=term_list, has_card=True,
                   hint="有卡就能进稿：write_linear(title=…, "
                        "entries=[{'arxiv_id': …, 'why': …}])")

    register(define_command(
        name="write_card", scope=("library",), side_effect=True,
        description="给一篇论文写卡：五段摘要 + 关键词。卡是「读过摘要」的物证。",
        parameters=_schema({"arxiv_id": _ID,
                            "tldr": {"type": "string", "description": "一句话总括"},
                            "problem": {"type": "string"}, "method": {"type": "string"},
                            "results": {"type": "string"}, "novelty": {"type": "string"},
                            "terms": {"type": "string",
                                      "description": "语义关键词，逗号分隔"}},
                           ["arxiv_id"]),
        output_schema=_out({"arxiv_id": {"type": "string"}, "terms": {"type": "array"},
                               "has_card": {"type": "boolean"}},
                              ["arxiv_id", "terms", "has_card"]),
    ), write_card)

    # ------------------------------------------------------------------ 人的动作
    def record_signal(arxiv_id: str, signal: str, source: str = "measured") -> CommandResult:
        """`source` 区分**实测**（面板埋点）与**声明**（AI 转述"用户喜欢这篇"）。

        两者同表同权参与画像，但审计里要分得清——"用户真点了"和"AI 说用户喜欢"
        可信度不同，将来要分档加权时这是唯一的依据。
        """
        aid = base_id(arxiv_id)
        if signal not in settings.signal_weights:
            raise MechaError(f"未知信号 {signal!r}", kind="bad_signal",
                             hint="可写：/".join(settings.signal_weights))
        if source not in ("measured", "declared"):
            raise MechaError(f"source 只能是 measured / declared，收到 {source!r}",
                             kind="bad_params")
        if repo.paper(aid) is None:
            raise MechaError(f"库里没有 {aid}", kind="not_found", hint="先 fetch_paper")
        repo.record(op=f"signal:{signal}", target=aid, actor=_actor(),
                    after={"weight": settings.signal_weights[signal], "source": source})
        return _ok(arxiv_id=aid, signal=signal, source=source,
                   hint="信号已记账；权重在下次 recompute_weights 或读画像时体现")

    register(define_command(
        name="record_signal", scope=("library",), side_effect=True,
        description="记一个兴趣信号（view/outbound/download/read/star/skip/uninterested）。"
                    "source=measured 表示面板实测，declared 表示你代用户声明。",
        parameters=_schema({"arxiv_id": _ID,
                            "signal": {"type": "string", "description": "信号名"},
                            "source": {"type": "string",
                                       "description": "measured（实测）/ declared（声明）"}},
                           ["arxiv_id", "signal"]),
        output_schema=_out({"arxiv_id": {"type": "string"}, "signal": {"type": "string"},
                            "source": {"type": "string"}},
                           ["arxiv_id", "signal", "source"]),
    ), record_signal)

    def _state(arxiv_id: str, field: str, value: bool, op: str) -> CommandResult:
        aid = base_id(arxiv_id)
        if repo.set_state(aid, field, value) is None:
            raise MechaError(f"库里没有 {aid}", kind="not_found", hint="先 fetch_paper")
        repo.record(op=op, target=aid, actor=_actor(), after={field: value})
        return _ok(arxiv_id=aid, **{field: value})

    def star_paper(arxiv_id: str, starred: bool = True) -> CommandResult:
        return _state(arxiv_id, "starred", starred, "star_paper")

    def mark_read(arxiv_id: str, read: bool = True) -> CommandResult:
        return _state(arxiv_id, "read", read, "mark_read")

    def skip_paper(arxiv_id: str) -> CommandResult:
        result = _state(arxiv_id, "skipped", True, "skip_paper")
        repo.record(op="signal:uninterested", target=base_id(arxiv_id), actor=_actor(),
                    after={"weight": settings.signal_weights["uninterested"]})
        return result

    register(define_command(
        name="star_paper", scope=("library",), side_effect=True,
        description="收藏 / 取消收藏（收藏也算一个正向信号）。",
        parameters=_schema({"arxiv_id": _ID, "starred": {"type": "boolean"}},
                           ["arxiv_id"]),
        output_schema=_out({"arxiv_id": {"type": "string"}, "starred": {"type": "boolean"}},
                              ["arxiv_id", "starred"]),
    ), star_paper)

    register(define_command(
        name="mark_read", scope=("library",), side_effect=True,
        description="标记已读 / 未读。",
        parameters=_schema({"arxiv_id": _ID, "read": {"type": "boolean"}}, ["arxiv_id"]),
        output_schema=_out({"arxiv_id": {"type": "string"}, "read": {"type": "boolean"}},
                              ["arxiv_id", "read"]),
    ), mark_read)

    register(define_command(
        name="skip_paper", scope=("library",), side_effect=True,
        description="标记不感兴趣（同时记一条负向信号）。",
        parameters=_schema({"arxiv_id": _ID}, ["arxiv_id"]),
        output_schema=_out({"arxiv_id": {"type": "string"}, "skipped": {"type": "boolean"}},
                              ["arxiv_id", "skipped"]),
    ), skip_paper)

    def add_note(arxiv_id: str, content: str) -> CommandResult:
        aid = base_id(arxiv_id)
        note_id = repo.add_note(aid, content)
        if note_id is None:
            raise MechaError(f"库里没有 {aid}", kind="not_found", hint="先 fetch_paper")
        repo.record(op="add_note", target=aid, actor=_actor(), after={"note_id": note_id})
        return _ok(arxiv_id=aid, note_id=note_id)

    register(define_command(
        name="add_note", scope=("library",), side_effect=True,
        description="给论文加一条笔记。",
        parameters=_schema({"arxiv_id": _ID, "content": {"type": "string"}},
                           ["arxiv_id", "content"]),
        output_schema=_out({"arxiv_id": {"type": "string"}, "note_id": {"type": "integer"}},
                              ["arxiv_id", "note_id"]),
    ), add_note)

    # ------------------------------------------------------------------ 兴趣
    def recompute_weights() -> CommandResult:
        stats = scoring.recompute_profile(repo, settings.signal_weights)
        repo.record(op="recompute_weights", actor=_actor(), after=stats)
        return _ok(**stats)

    register(define_command(
        name="recompute_weights", scope=("profile",), side_effect=True,
        description="从信号账本全量重建画像权重（锚点不动）。",
        parameters=_schema({}, []),
        output_schema=_out({"signals": {"type": "integer"}, "keys": {"type": "integer"},
                               "missing_papers": {"type": "array"}},
                              ["signals", "keys"]),
    ), recompute_weights)

    def add_anchor(dimension: str, key: str, weight: float = 1.0) -> CommandResult:
        try:
            scoring.set_anchor(repo, dimension=dimension, key=key, weight=float(weight))
        except ValueError as exc:
            raise MechaError(str(exc), kind="bad_dimension",
                             hint="dimension 只能是 category / term / author") from exc
        repo.record(op="add_anchor", actor=_actor(),
                    after={"dimension": dimension, "key": key, "weight": float(weight)})
        return _ok(dimension=dimension, key=key, weight=float(weight))

    register(define_command(
        name="add_anchor", scope=("profile",), side_effect=True,
        description="钉一个人工权重（不衰减、不参与重算）。",
        parameters=_schema({"dimension": {"type": "string",
                                         "description": "category / term / author"},
                            "key": {"type": "string"}, "weight": {"type": "number"}},
                           ["dimension", "key"]),
        output_schema=_out({"dimension": {"type": "string"}, "key": {"type": "string"},
                               "weight": {"type": "number"}},
                              ["dimension", "key", "weight"]),
    ), add_anchor)

    def remove_anchor(dimension: str, key: str) -> CommandResult:
        removed = scoring.remove_anchor(repo, dimension=dimension, key=key)
        repo.record(op="remove_anchor", actor=_actor(),
                    after={"dimension": dimension, "key": key, "removed": removed})
        return _ok(dimension=dimension, key=key, removed=removed)

    register(define_command(
        name="remove_anchor", scope=("profile",), side_effect=True,
        description="删掉一个人工权重。",
        parameters=_schema({"dimension": {"type": "string"}, "key": {"type": "string"}},
                           ["dimension", "key"]),
        output_schema=_out({"dimension": {"type": "string"}, "key": {"type": "string"},
                               "removed": {"type": "boolean"}},
                              ["dimension", "key", "removed"]),
    ), remove_anchor)

    # ------------------------------------------------------------------ 草稿
    #
    # 为什么拆成两条命令（而不是一条 `write_draft(kind=…, payload=…)`）：
    # `payload` 是个"按 kind 变形的袋子"，schema 管不了，AI 只能靠描述猜键名。
    # 拆开后每条命令的参数就是真 schema：数组、对象各归各位，校验与教学都在位。
    # 又为什么都要 `mode`：整份替换意味着**改一个字也要把整张图重说一遍**，
    # 而且几十个元素一次生成、中途截断就整份失败。增量只发这一批，断了丢一批。
    def _save_draft(kind: str, title: str, incoming: dict, note: str, draft_id: int,
                    mode: str, remove: list, op: str) -> CommandResult:
        title = (title or "").strip()
        if not title:
            raise MechaError("title 不能为空", kind="bad_params",
                             hint="草稿名就是情景，如「光镊搬运近三个月」")
        mode = (mode or "merge").strip().lower()
        if mode not in ("merge", "replace"):
            raise MechaError(f"mode 只能是 merge / replace，收到 {mode!r}",
                             kind="bad_params",
                             hint="merge=只发这一批（默认）；replace=整份替换")
        if isinstance(incoming, str):                 # 另一侧可能把 JSON 当字符串送来
            try:
                incoming = json.loads(incoming) if incoming.strip() else {}
            except ValueError as exc:
                raise MechaError(f"载荷不是合法 JSON：{exc}", kind="bad_params") from exc
        incoming = incoming if isinstance(incoming, dict) else {}
        ident = int(draft_id or 0)
        # 按 id 就只按 id；不给 id 就只按标题找——**纯数字标题（「1024」）不能被当成 id**
        found = repo.draft(ident) if ident else repo.draft_by_title(title)
        if found is not None and found["kind"] != kind:
            raise MechaError(f"「{found['title']}」是 {found['kind']} 草稿，不能写成 {kind}",
                             kind="bad_params",
                             hint="换个 title，或先 rename_draft 腾出这个名字")
        existing = (found["payload"] or {}) if (mode == "merge" and found) else None
        ident = ident or (int(found["id"]) if found else 0)
        merged = (draft_lib.apply_merge(kind, existing, incoming, remove)
                  if mode == "merge" else incoming)
        try:
            clean = draft_lib.normalize(kind, merged, repo)
        except draft_lib.DraftError as exc:
            hint = ("merge 时只发新增/改动的；要删必须写进 remove（没提到≠删掉）"
                    if mode == "merge" else "先 read_draft 看已有形状，按回执改")
            raise MechaError(str(exc), kind="bad_draft", hint=hint) from exc
        checks = draft_lib.checkup(repo, kind, clean,
                                   recent_days=settings.recent_draft_days,
                                   exclude_draft_id=ident or None)
        ident = repo.save_draft(kind=kind, title=title, note=note or "", payload=clean,
                                author=_actor(), draft_id=ident or None)
        repo.record(op=op, target=str(ident), actor=_actor(),
                    after={"kind": kind, "title": title, "mode": mode,
                           "count": checks["total"], "elements": checks["elements"],
                           "removed": len(remove or [])})
        return _ok(draft_id=ident, kind=kind, title=title, mode=mode, checkup=checks)

    def write_linear(title: str = "", entries: list | None = None, note: str = "",
                     draft_id: int = 0, mode: str = "merge",
                     remove: list | None = None) -> CommandResult:
        return _save_draft("linear", title, {"entries": entries or []}, note,
                           draft_id, mode, list(remove or []), "write_linear")

    register(define_command(
        name="write_linear", scope=("drafts",), side_effect=True,
        description="写线性草稿（条目 + 一句 why）。**支持增量**：默认 mode=merge，"
                    "只发新增或改动的条目，按 arxiv_id 认人（重复=就地更新，新的追加到末尾，"
                    "数组顺序就是页面顺序）；要删必须写进 remove。mode=replace 才整份替换。",
        parameters=_schema({"title": {"type": "string", "description": "草稿名=情景"},
                            "entries": {"type": "array",
                                        "description": "[{arxiv_id, why, group?}]；"
                                                       "merge 时只发这一批"},
                            "note": {"type": "string"},
                            "draft_id": {"type": "integer",
                                         "description": "指定那一份；省略=按 title 找同名"},
                            "mode": {"type": "string",
                                     "description": "merge（默认，增量）/ replace（整份替换）"},
                            "remove": {"type": "array",
                                       "description": "要删掉的 arxiv_id 列表"}},
                           ["title"]),
        output_schema=_out({"draft_id": {"type": "integer"}, "kind": {"type": "string"},
                               "title": {"type": "string"}, "mode": {"type": "string"},
                               "checkup": {"type": "object"}},
                              ["draft_id", "kind", "title", "mode", "checkup"]),
    ), write_linear)

    def write_network(title: str = "", nodes: list | None = None, edges: list | None = None,
                      layout: dict | None = None, note: str = "", draft_id: int = 0,
                      mode: str = "merge", remove: list | None = None) -> CommandResult:
        return _save_draft("network", title,
                           {"nodes": nodes or [], "edges": edges or [],
                            "layout": layout or {}},
                           note, draft_id, mode, list(remove or []), "write_network")

    register(define_command(
        name="write_network", scope=("drafts",), side_effect=True,
        description="写网络草稿（自由画布：论文/文字/便签/框混排，坐标由 Graphviz 算）。"
                    "**支持增量**：默认 mode=merge，只发新增或改动的节点与边，节点按 id、"
                    "边按 (src,dst) 认人；要删必须写进 remove。论文节点只写 {id, arxiv_id} "
                    "即可，label 省略时从库里补标题（必须在库，点开进详情页）。"
                    "kind 只决定缺省样式：paper / text / note / group（用 parent 框住别人）/ badge，"
                    "形状可另给 shape（Graphviz 任意 shape 名）。",
        parameters=_schema({"title": {"type": "string", "description": "草稿名=情景"},
                            "nodes": {"type": "array",
                                      "description": "[{id, kind, label?, arxiv_id?, parent?, "
                                                     "shape?, rank?}]；merge 时只发这一批"},
                            "edges": {"type": "array",
                                      "description": "[{src, dst, label?, style?, arrow?}]；"
                                                     "style: solid|dashed|dotted，arrow: normal|none"},
                            "layout": {"type": "object",
                                       "description": "{rankdir: 'TB'|'LR'}"},
                            "note": {"type": "string"},
                            "draft_id": {"type": "integer",
                                         "description": "指定那一份；省略=按 title 找同名"},
                            "mode": {"type": "string",
                                     "description": "merge（默认，增量）/ replace（整份替换）"},
                            "remove": {"type": "array",
                                       "description": "要删掉的节点 id 列表"}},
                           ["title"]),
        output_schema=_out({"draft_id": {"type": "integer"}, "kind": {"type": "string"},
                               "title": {"type": "string"}, "mode": {"type": "string"},
                               "checkup": {"type": "object"}},
                              ["draft_id", "kind", "title", "mode", "checkup"]),
    ), write_network)

    def rename_draft(draft_id: int, title: str) -> CommandResult:
        if not repo.rename_draft(int(draft_id), title.strip()):
            raise MechaError("改名失败：可能已有同名草稿", kind="bad_params",
                             hint="先 query_drafts 看现有名字")
        repo.record(op="rename_draft", actor=_actor(),
                    after={"id": int(draft_id), "title": title})
        return _ok(draft_id=int(draft_id), title=title.strip())

    register(define_command(
        name="rename_draft", scope=("drafts",), side_effect=True,
        description="给草稿改名。",
        parameters=_schema({"draft_id": {"type": "integer"}, "title": {"type": "string"}},
                           ["draft_id", "title"]),
        output_schema=_out({"draft_id": {"type": "integer"}, "title": {"type": "string"}},
                              ["draft_id", "title"]),
    ), rename_draft)

    def delete_draft(draft_id: int) -> CommandResult:
        removed = repo.delete_draft(int(draft_id))
        repo.record(op="delete_draft", actor=_actor(),
                    after={"id": int(draft_id), "removed": removed})
        return _ok(draft_id=int(draft_id), removed=removed)

    register(define_command(
        name="delete_draft", scope=("drafts",), side_effect=True,
        description="删除草稿。",
        parameters=_schema({"draft_id": {"type": "integer"}}, ["draft_id"]),
        output_schema=_out({"draft_id": {"type": "integer"},
                               "removed": {"type": "boolean"}},
                              ["draft_id", "removed"]),
    ), delete_draft)

    # ------------------------------------------------------------------ 引文
    def _sync(arxiv_id: str, limit: int, *, incoming: bool) -> CommandResult:
        from ..scholar import ScholarError
        aid = base_id(arxiv_id)
        client = ctx.scholar()
        try:
            rows = (client.citations(aid, limit=int(limit)) if incoming
                    else client.references(aid, limit=int(limit)))
        except ScholarError as exc:
            raise MechaError(str(exc), kind="scholar_unavailable",
                             hint="Semantic Scholar 限速或没这篇；稍后重试或调小 limit") from exc
        finally:
            client.close()
        stats = (repo.merge_incoming(aid, rows) if incoming
                 else repo.replace_edges(aid, rows))
        repo.record(op="sync_cited_by" if incoming else "sync_citations",
                    target=aid, actor=_actor(), after=stats)
        return _ok(**stats)

    def sync_citations(arxiv_id: str, limit: int = 40) -> CommandResult:
        return _sync(arxiv_id, limit, incoming=False)

    def sync_cited_by(arxiv_id: str, limit: int = 40) -> CommandResult:
        return _sync(arxiv_id, limit, incoming=True)

    register(define_command(
        name="sync_citations", scope=("citations",), side_effect=True,
        description="把这一篇的参考文献拉进图（幂等，重跑覆盖它自己的边）。",
        parameters=_schema({"arxiv_id": _ID, "limit": {"type": "integer"}}, ["arxiv_id"]),
        output_schema=_out({"src": {"type": "string"}, "kind": {"type": "string"},
                               "removed": {"type": "integer"}, "added": {"type": "integer"}},
                              ["src", "kind", "added"]),
    ), sync_citations)

    register(define_command(
        name="sync_cited_by", scope=("citations",), side_effect=True,
        description="把「谁引用了这一篇」拉进图（增量，不删引用者自己的边）。",
        parameters=_schema({"arxiv_id": _ID, "limit": {"type": "integer"}}, ["arxiv_id"]),
        output_schema=_out({"dst": {"type": "string"}, "kind": {"type": "string"},
                               "added": {"type": "integer"}},
                              ["dst", "kind", "added"]),
    ), sync_cited_by)

    # ------------------------------------------------------------------ 页面偏好
    def set_default_draft(kind: str, draft_id: int = 0) -> CommandResult:
        if kind not in ("linear", "network"):
            raise MechaError(f"kind 只能是 linear / network，收到 {kind!r}",
                             kind="bad_params")
        repo.set_setting(f"page.{kind}.default", str(int(draft_id)) if int(draft_id) else "")
        repo.record(op="set_default_draft", actor=_actor(),
                    after={"kind": kind, "draft_id": int(draft_id)})
        return _ok(kind=kind, draft_id=int(draft_id))

    register(define_command(
        name="set_default_draft", scope=("drafts",), side_effect=True,
        description="指定某个展示页默认显示哪一份草稿（0 = 自动取最近一份）。",
        parameters=_schema({"kind": {"type": "string", "description": "linear / network"},
                            "draft_id": {"type": "integer"}},
                           ["kind"]),
        output_schema=_out({"kind": {"type": "string"}, "draft_id": {"type": "integer"}},
                              ["kind", "draft_id"]),
    ), set_default_draft)

    # ------------------------------------------------------------------ 配置
    def set_config(key: str, value: float, channel=None) -> CommandResult:
        """配置也要过 Gate：先校验、再落史、再装回进程。"""
        if key not in config_mod.CONFIG_SCHEMA:
            raise MechaError(f"{key!r} 不是可写配置", kind="unknown_key",
                             hint="可写键见 read_config")
        cast = config_mod.coerce(key, value)
        software.gate.set(channel, key, cast, reason="set_config")
        config_mod.apply_snapshot(settings, {key: cast})
        return _ok(key=key, value=cast, hint="立即生效；read_config 可回读")

    register(define_command(
        name="set_config", scope=("config",), side_effect=True, wants_channel=True,
        description="改一个标量配置（立即生效，并作为域状态留史）。",
        parameters=_schema({"key": {"type": "string", "description": "配置键"},
                            "value": {"type": "number", "description": "新值"}},
                           ["key", "value"]),
        output_schema=_out({"key": {"type": "string"}, "value": {"type": "number"}},
                              ["key", "value"]),
    ), set_config)

    return names
