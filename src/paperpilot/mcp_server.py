"""PaperPilot MCP 语义通道（localhost）——把论文情报面暴露给 DSH 等 MCP harness。

架构（DESIGN.md §17，对齐 Energy Level `src/mecha/server.py` 的成熟模式）：

    DSH（AI 对话/管理/UI，复用） ──MCP(streamable-http, 仅 localhost)──▶ 本模块
                                                              │
                                              工具 = repo/service 的薄封装
                                              （不含业务逻辑副本）

三条纪律（从 Energy Level 移植）：
1. **可教学错误**：任何失败 → `{ok:false, error:{kind,hint,suggest}}`，第一次错就能改对；
2. **回程体积闸**：每个回程过 `_gate`，超限截断并带「截了多少/完整数据去哪看」；
3. **人机同路径**：MCP 工具与 CLI/Web 调同一批 service 方法（run/finalize 与定时任务同源）。

发现机制（T4：harness 绝不拉起权威）：launcher（`paperpilot ai`）起服务并写
`.mcp-port` 端口文件，DSH 插件据此 attach。工具函数带真实签名（MCPServer 从签名
推输入 schema）；`create_server` 同时返回 tools_dict，判据/测试可直接调用而无需起 HTTP。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from datetime import datetime, timedelta

from .infra.ai.errors import AIError
from .infra.arxiv import ArxivClient, ArxivError

logger = logging.getLogger("paperpilot.mcp")

__all__ = [
    "TOOL_NAMES",
    "create_server",
    "find_free_port",
    "write_port_file",
    "read_port_file",
    "port_file_path",
    "run_streamable_http",
    "DEFAULT_MCP_PORT",
]

DEFAULT_MCP_PORT = 8780
ACTOR = "ai"

TOOL_NAMES = [
    "list_topics", "get_digest", "search_papers", "get_paper", "fetch_papers",
    "prepare_review", "submit_review", "finalize_briefing", "review_status",
    "run_pipeline", "add_topic", "set_topic_enabled",
    "mark_read", "star_paper", "skip_paper", "add_note", "get_activity", "undo",
]

_INSTRUCTIONS = (
    "PaperPilot 论文情报语义通道。每日简报的 AI 协作流程："
    "① fetch_papers 抓取新论文入库（遵守 arXiv 限速，稍慢）；"
    "② prepare_review 取当过规则后的候选清单（含主题画像与基线分）；"
    "③ 你逐篇评审（score/label/reason，入选者给结构化 summary），"
    "submit_review 一次性提交；④ finalize_briefing 生成简报。"
    "也可 run_pipeline 一键跑（无 AI 参与时用 heuristic 兜底）。"
    "检索用 search_papers/get_paper；主题管理用 add_topic/set_topic_enabled。"
    "【归因与记录仪】所有写入工具接受 reason（为什么）；写入带 actor='ai' 记进"
    "append-only 事件总线，get_activity(since_seq/actor/op) 可查'谁改了什么'；"
    "写错了用 undo(seq=0) 撤销最近一条可逆操作（入库/定稿不可逆，会明说）。"
    "所有错误可教学（kind/hint/suggest），按提示改，别盲试。"
)

# 回程预算（字节）：超过则截断列表并告知去哪看全量
_RETURN_BUDGET = 65536
_LIST_KEEP = 20


# =====================================================================
# 端口文件发现（harness attach 用）
# =====================================================================
def port_file_path(container=None) -> str:
    if container is not None:
        return os.path.join(str(container.settings.data_dir.parent), ".mcp-port")
    return os.path.join(os.getcwd(), ".mcp-port")


def find_free_port(preferred: int = DEFAULT_MCP_PORT) -> int:
    """取可用端口：preferred 空闲则用之，否则让 OS 分配。"""
    for port in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return int(s.getsockname()[1])
        except OSError:
            continue
    raise RuntimeError("找不到可用端口")


def write_port_file(port: int, path: str | None = None, container=None) -> str:
    path = path or port_file_path(container)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(int(port)))
    return path


def read_port_file(path: str | None = None, container=None) -> int | None:
    path = path or port_file_path(container)
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


# =====================================================================
# 体积闸 + 可教学错误回程
# =====================================================================
def _gate(obj, budget: int = _RETURN_BUDGET) -> dict:
    """回程体积闸：超预算时截断长列表，带 kept/total/where（不静默）。"""
    if isinstance(obj, dict):
        for key in ("items", "papers", "candidates", "archived", "briefings", "topics"):
            value = obj.get(key)
            if isinstance(value, list) and len(value) > _LIST_KEEP:
                obj[key] = value[:_LIST_KEEP]
                obj[f"_{key}_truncated"] = {
                    "kept": _LIST_KEEP,
                    "total": len(value),
                    "where": "全量数据见 Web 面板 /papers 或 get_paper(arxiv_id)",
                }
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) <= budget:
        return obj
    return {
        "ok": obj.get("ok", True),
        "truncated": True,
        "kept_bytes": budget,
        "total_bytes": len(text.encode("utf-8")),
        "where": "回程超体积闸：缩小 limit/days，或改看 Web 面板",
        "preview": text[:2000],
    }


def _emit(obj) -> str:
    return json.dumps(_gate(obj), ensure_ascii=False, default=str)


def _safe(thunk) -> str:
    """执行 thunk → 过闸 → JSON；AIError/异常 → 可教学错误体（不 500）。

    P2（GAPS.md §5）：兜底 except 也必须带 hint——未分类错误回程同样可教学。
    """
    try:
        return _emit(thunk())
    except AIError as exc:
        return _emit({"ok": False, "error": exc.to_dict()})
    except ArxivError as exc:
        return _emit(
            {"ok": False, "error": {"kind": "arxiv_unavailable", "message": str(exc),
                                    "hint": "arXiv 限速或网络问题：稍后重试，或调小 --days"}}
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP 工具执行失败")
        return _emit(
            {"ok": False, "error": {
                "kind": type(exc).__name__,
                "message": str(exc),
                "hint": "未分类错误：请附上本次调用的工具名与参数重试；"
                        "先用 search_papers / get_paper / list_topics 核对输入是否存在",
            }}
        )


# =====================================================================
# 建 server（工具 = service 的薄封装）
# =====================================================================
def create_server(container):
    """→ (MCPServer, tools_dict)。tools_dict[name] 是已注册工具函数（判据可直调）。"""
    from mcp.server.mcpserver import MCPServer

    from .config import TopicCfg, save_settings
    from .domain.pipeline import DailyPipelineService  # noqa: F401  （类型注释用）

    repo = container.repo
    pipeline = container.pipeline
    retrieval = container.retrieval
    settings = container.settings

    srv = MCPServer("paperpilot", instructions=_INSTRUCTIONS)

    # ---------------------------------------------------------------- 只读面
    @srv.tool(name="list_topics", description="列出研究主题（名称/关键词/分类白名单/配额/阈值/启用态）。")
    def list_topics() -> str:
        return _safe(lambda: {
            "ok": True,
            "topics": [
                {
                    "name": t.name,
                    "description": t.description,
                    "keywords": list(t.keywords or []),
                    "categories": list(t.categories or []),
                    "exclude_keywords": list(t.exclude_keywords or []),
                    "quota": t.quota,
                    "threshold": t.threshold,
                    "enabled": t.enabled,
                }
                for t in settings.topics
            ],
        })

    @srv.tool(
        name="get_digest",
        description="某日简报全文（Markdown）+ 统计。date 空 = 今天；full=False 只回统计与条目。",
    )
    def get_digest(date_str: str = "", full: bool = True) -> str:
        def _do():
            target = date_str or datetime.now().date().isoformat()
            briefing = repo.briefing_for_date(target)
            if briefing is None:
                return {
                    "ok": True,
                    "date": target,
                    "exists": False,
                    "hint": "当天没有简报；先 fetch_papers + prepare_review/submit_review/finalize_briefing，或 run_pipeline",
                }
            stats = briefing.stats or {}
            out = {
                "ok": True,
                "date": target,
                "exists": True,
                "title": briefing.title,
                "ai_enabled": briefing.ai_enabled,
                "stats": stats.get("stats", {}),
                "items": stats.get("items", []),
                "archived_count": len(stats.get("archived", [])),
            }
            if full:
                out["markdown"] = briefing.markdown
            return out

        return _safe(_do)

    @srv.tool(
        name="search_papers",
        description="论文库检索（FTS5：标题/摘要/TL;DR）。query 空则按时间倒序列近期论文。",
    )
    def search_papers(query: str = "", label: str = "", category: str = "", limit: int = 20) -> str:
        def _do():
            papers = retrieval.search(
                query, label=label or None, primary_category=category or None,
                limit=max(1, min(int(limit), 100)),
            )
            return {
                "ok": True,
                "count": len(papers),
                "papers": [
                    {
                        "arxiv_id": p.arxiv_id,
                        "title": p.title,
                        "primary_category": p.primary_category,
                        "published_at": p.published_at.isoformat() if p.published_at else None,
                        "status": p.status,
                        "score": p.scores[-1].score if p.scores else None,
                        "label": p.scores[-1].label if p.scores else None,
                        "star": bool(p.reading and p.reading.star),
                    }
                    for p in papers
                ],
            }

        return _safe(_do)

    @srv.tool(name="get_paper", description="论文详情：原文摘要 + 最新 AI 总结 + 打分历史 + 笔记。")
    def get_paper(arxiv_id: str) -> str:
        def _do():
            detail = retrieval.detail(arxiv_id)
            if detail is None:
                return {"ok": False, "error": {"kind": "not_found",
                                               "message": f"找不到论文 {arxiv_id}",
                                               "hint": "先用 search_papers 搜到正确 arxiv_id"}}
            p, s = detail["paper"], detail["summary"]
            return {
                "ok": True,
                "paper": {
                    "arxiv_id": p.arxiv_id,
                    "title": p.title,
                    "authors": list(p.authors or []),
                    "categories": list(p.categories or []),
                    "primary_category": p.primary_category,
                    "published_at": p.published_at.isoformat() if p.published_at else None,
                    "abs_url": p.abs_url,
                    "pdf_url": p.pdf_url,
                    "status": p.status,
                    "abstract": p.abstract,
                },
                "summary": (
                    {"tldr": s.tldr, "problem": s.problem, "method": s.method,
                     "results": s.results, "novelty": s.novelty,
                     "keywords": list(s.keywords or []), "model": s.model}
                    if s else None
                ),
                "scores": [
                    {"score": sc.score, "label": sc.label, "reason": sc.reason,
                     "model": sc.model, "run_id": sc.run_id}
                    for sc in detail["scores"]
                ],
                "notes": [n.content for n in detail["notes"]],
                "reading": (
                    {"read": detail["reading"].read, "star": detail["reading"].star,
                     "marked_skip": detail["reading"].marked_skip}
                    if detail["reading"] else None
                ),
            }

        return _safe(_do)

    @srv.tool(
        name="get_activity",
        description="归因面（只读）：① 事件总线 diff-since-seq（actor/op 过滤，append-only 记录仪）；"
                    "② 近期运行/AI 调用/简报统计。events 是主答案，其余是背景。",
    )
    def get_activity(
        since_seq: int = 0,
        actor: str = "",
        op: str = "",
        days: int = 7,
        limit: int = 50,
    ) -> str:
        def _do():
            events = repo.events_since(
                since_seq=since_seq, actor=actor, op=op, limit=limit
            )
            since = datetime.utcnow() - timedelta(days=max(1, int(days)))
            calls = repo.ai_calls_since(since, limit=50)
            briefings = repo.briefings(limit=14)
            return {
                "ok": True,
                # ① 事件总线（L2 记录仪的读面：谁、何时、为什么、改了什么）
                "events": events["events"],
                "events_last_seq": events["last_seq"],
                "events_count": events["count"],
                "events_filters": {"since_seq": since_seq, "actor": actor, "op": op},
                # ② 背景统计
                "counts_by_status": repo.counts_by_status(),
                "recent_briefings": [
                    {"date": b.date, "title": b.title, "status": b.status,
                     "ai_enabled": b.ai_enabled}
                    for b in briefings
                ],
                "ai_calls": [
                    {"ts": c.ts.isoformat(), "port": c.port, "purpose": c.purpose,
                     "model": c.model, "latency_ms": c.latency_ms, "tokens": c.tokens,
                     "ok": c.ok, "error": c.error}
                    for c in calls
                ],
            }

        return _safe(_do)

    @srv.tool(
        name="undo",
        description="撤销一条可逆写入（seq=0 = 最近一条可逆事件）。按事件里的 before 快照回写；"
                    "不可逆操作（抓取入库/简报定稿）会明确拒绝并说明原因。undo 本身也记一条事件。",
    )
    def undo(seq: int = 0, reason: str = "") -> str:
        return _safe(lambda: repo.undo(int(seq), actor=ACTOR, reason=reason))

    # ---------------------------------------------------------------- 写入/运行面
    @srv.tool(
        name="fetch_papers",
        description="按主题抓取 arXiv 最近 N 天提交的新论文入库（遵守 3s 限速，可能较慢）。",
    )
    def fetch_papers(days: int = 3, reason: str = "") -> str:
        def _do():
            client = ArxivClient(cache_dir=settings.cache_dir / "arxiv")
            try:
                papers = client.fetch_candidates(
                    topics=settings.topics,
                    categories=settings.arxiv_categories,
                    since=datetime.utcnow() - timedelta(days=max(1, int(days))),
                )
            finally:
                client.close()
            result = repo.upsert_papers(
                papers, actor=ACTOR, reason=reason or f"抓取最近 {days} 天论文"
            )
            return {
                "ok": True,
                "fetched": len(papers),
                "new": result["new"],
                "updated": result["updated"],
                "hint": "接着 prepare_review 生成待评审候选",
            }

        return _safe(_do)

    @srv.tool(
        name="prepare_review",
        description="阶段1：取当过规则后的候选清单（含主题画像+摘要截断+基线分），等你评审。",
    )
    def prepare_review(date_str: str = "", reason: str = "") -> str:
        def _do():
            from datetime import date as date_cls

            when = date_cls.fromisoformat(date_str) if date_str else None
            prepared = pipeline.prepare_review(
                when, actor=ACTOR, reason=reason or "AI 请求评审候选"
            )
            return {"ok": True, **prepared.payload}

        return _safe(_do)

    @srv.tool(
        name="submit_review",
        description="阶段2：提交你对候选的评审。reviews: [{arxiv_id, score, label, reason, tags?, summary?}]。",
    )
    def submit_review(date_str: str, reviews: list) -> str:
        return _safe(lambda: pipeline.submit_review(date_str, list(reviews or [])))

    @srv.tool(
        name="finalize_briefing",
        description="阶段3：用已提交评审（缺的用基线分）做筛选、精读、生成简报并落库。",
    )
    def finalize_briefing(date_str: str = "", force: bool = False, reason: str = "") -> str:
        def _do():
            from datetime import date as date_cls

            when = date_cls.fromisoformat(date_str) if date_str else None
            result = pipeline.finalize_review(
                when, force=force, actor=ACTOR, reason=reason or "AI 定稿简报"
            )
            if result.error:
                return {"ok": False, "error": {"kind": "finalize_failed",
                                               "message": result.error}}
            return {
                "ok": True,
                "date": result.date,
                "run_id": result.run_id,
                "fetched": result.fetched,
                "after_rules": result.after_rules,
                "selected": result.selected,
                "reused": result.reused,
                "briefing_id": result.briefing_id,
                "degraded": result.degraded,
            }

        return _safe(_do)

    @srv.tool(name="review_status", description="看某天评审进度（候选数/已评审数/状态）。")
    def review_status(date_str: str = "") -> str:
        return _safe(lambda: pipeline.review_status(date_str or datetime.now().date().isoformat()))

    @srv.tool(
        name="run_pipeline",
        description="一键全流程（无外部评审）：候选→规则→程序化 AI 档（未配 key 时 heuristic）→简报。",
    )
    def run_pipeline(date_str: str = "", force: bool = False, reason: str = "") -> str:
        def _do():
            from datetime import date as date_cls

            when = date_cls.fromisoformat(date_str) if date_str else None
            result = pipeline.run(
                when, force=force, actor=ACTOR, reason=reason or "AI 一键跑流水线"
            )
            if result.error:
                return {"ok": False, "error": {"kind": "pipeline_failed",
                                               "message": result.error}}
            return {
                "ok": True,
                "date": result.date,
                "run_id": result.run_id,
                "fetched": result.fetched,
                "after_rules": result.after_rules,
                "selected": result.selected,
                "reused": result.reused,
                "degraded": result.degraded,
            }

        return _safe(_do)

    @srv.tool(name="add_topic", description="新增研究主题（写回 config/settings.yaml，即时生效）。")
    def add_topic(
        name: str,
        keywords: str = "",
        categories: str = "",
        description: str = "",
        exclude_keywords: str = "",
        quota: int = 4,
        threshold: float = 0.6,
        reason: str = "",
    ) -> str:
        def _do():
            if any(t.name == name for t in settings.topics):
                return {"ok": False, "error": {
                    "kind": "duplicate", "message": f"主题「{name}」已存在",
                    "suggest": [t.name for t in settings.topics][:5]}}
            topic = TopicCfg(
                name=name, description=description,
                keywords=_split(keywords), categories=_split(categories),
                exclude_keywords=_split(exclude_keywords),
                quota=quota, threshold=threshold, enabled=True,
            )
            settings.topics = [*settings.topics, topic]
            save_settings(settings)
            repo.sync_topics(
                settings.topics, actor=ACTOR, reason=reason or f"AI 新增主题「{name}」"
            )
            return {"ok": True, "added": name, "total_topics": len(settings.topics)}

        return _safe(_do)

    @srv.tool(name="set_topic_enabled", description="启用/停用某主题（写回 YAML）。")
    def set_topic_enabled(name: str, enabled: bool, reason: str = "") -> str:
        def _do():
            for t in settings.topics:
                if t.name == name:
                    t.enabled = bool(enabled)
                    save_settings(settings)
                    repo.sync_topics(
                        settings.topics, actor=ACTOR,
                        reason=reason or f"AI {'启用' if enabled else '停用'}主题「{name}」",
                    )
                    return {"ok": True, "topic": name, "enabled": bool(enabled)}
            return {"ok": False, "error": {
                "kind": "unknown_topic", "message": f"没有主题「{name}」",
                "suggest": [t.name for t in settings.topics][:5]}}

        return _safe(_do)

    # ---------------------------------------------------------------- 阅读态
    @srv.tool(name="mark_read", description="标记论文已读/未读。")
    def mark_read(arxiv_id: str, read: bool = True, reason: str = "") -> str:
        return _safe(lambda: _paper_action(
            arxiv_id,
            lambda p: repo.set_read(p, read=read, actor=ACTOR, reason=reason),
        ))

    @srv.tool(name="star_paper", description="收藏/取消收藏论文。")
    def star_paper(arxiv_id: str, reason: str = "") -> str:
        def _do():
            paper = repo.get_paper(arxiv_id)
            if paper is None:
                return {"ok": False, "error": {"kind": "not_found",
                                               "message": f"找不到论文 {arxiv_id}",
                                               "hint": "先用 search_papers 搜到正确 arxiv_id"}}
            starred = repo.toggle_star(paper, actor=ACTOR, reason=reason)
            return {"ok": True, "arxiv_id": arxiv_id, "star": starred}

        return _safe(_do)

    @srv.tool(name="skip_paper", description="标记不感兴趣（同类下次过滤）。")
    def skip_paper(arxiv_id: str, reason: str = "") -> str:
        return _safe(lambda: _paper_action(
            arxiv_id,
            lambda p: repo.set_marked_skip(p, skip=True, actor=ACTOR, reason=reason),
        ))

    @srv.tool(name="add_note", description="给论文加笔记（你的调研沉淀）。")
    def add_note(arxiv_id: str, content: str, reason: str = "") -> str:
        return _safe(lambda: _paper_action(
            arxiv_id,
            lambda p: repo.add_note(p, content, actor=ACTOR, reason=reason),
        ))

    def _paper_action(arxiv_id: str, action) -> dict:
        paper = repo.get_paper(arxiv_id)
        if paper is None:
            return {"ok": False, "error": {"kind": "not_found",
                                           "message": f"找不到论文 {arxiv_id}",
                                           "hint": "先用 search_papers 搜到正确 arxiv_id"}}
        action(paper)
        return {"ok": True, "arxiv_id": arxiv_id}

    tools = {name: fn for name, fn in [
        ("list_topics", list_topics), ("get_digest", get_digest),
        ("search_papers", search_papers), ("get_paper", get_paper),
        ("get_activity", get_activity), ("fetch_papers", fetch_papers),
        ("prepare_review", prepare_review), ("submit_review", submit_review),
        ("finalize_briefing", finalize_briefing), ("review_status", review_status),
        ("run_pipeline", run_pipeline), ("add_topic", add_topic),
        ("set_topic_enabled", set_topic_enabled), ("mark_read", mark_read),
        ("star_paper", star_paper), ("skip_paper", skip_paper),
        ("add_note", add_note), ("undo", undo),
    ]}
    return srv, tools


def _split(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


# =====================================================================
# 运行 + launcher
# =====================================================================
async def run_streamable_http(srv, host: str = "127.0.0.1", port: int = DEFAULT_MCP_PORT) -> None:
    await srv.run_streamable_http_async(host=host, port=port, streamable_http_path="/mcp")


def main(argv=None) -> int:
    """人运行的入口：`python -m paperpilot.mcp_server [--port N] [--stdio]`。"""
    import argparse

    from .app.container import build_container
    from .config import load_settings

    ap = argparse.ArgumentParser(prog="paperpilot-mcp")
    ap.add_argument("--port", type=int, default=DEFAULT_MCP_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config", default=None)
    ap.add_argument("--stdio", action="store_true", help="用 stdio 传输（默认 streamable-http）")
    args = ap.parse_args(argv)

    container = build_container(load_settings(args.config))
    srv, _tools = create_server(container)

    if args.stdio:
        logger.info("MCP 语义通道：stdio 传输")
        asyncio.run(srv.run_stdio_async())
        return 0

    port = find_free_port(args.port)
    write_port_file(port, container=container)
    logger.info("MCP 语义通道：http://%s:%d/mcp（端口文件 %s）", args.host, port,
                port_file_path(container))
    try:
        asyncio.run(run_streamable_http(srv, args.host, port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
