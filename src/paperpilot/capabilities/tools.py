"""能力工具集：把既有 service 薄封装成中性、自描述、可外部调用的能力。

**逻辑不重写**——全部委托 container 里的 repo/pipeline/retrieval/settings（单一事实源，
docs/PRINCIPLES.md §9）。读写分类与归因纪律见 docs/SPEC.md §3、§6：
- 只读能力（kind="read"）：外部可自由调用；
- 写入能力（kind="write"）：接受 actor/reason，落 append-only 事件总线，可 undo。
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta

from ..config import TopicCfg, save_settings
from ..infra.arxiv import ArxivClient
from ..infra.scholar import SemanticScholarClient, arxiv_ext_id
from .base import Registry, err, ok

# 外部调用方未表明身份时的默认归因（CLI/人可显式传 actor="human"）
ACTOR_DEFAULT = "ai"

#: 参数描述的唯一来源（N8）：按**能力名**（非 mecha 投影名）组织；mecha 层从
#: ToolSpec.params 透传，不再自备第二份（两份必漂移）。actor 由投影层隐去，不描述。
PARAM_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "get_digest": {"date": "简报日期 ISO 格式（省略=今天）",
                   "full": "True 含 Markdown 全文；False 只回统计与条目"},
    "search_papers": {"query": "检索词（省略=列近期）", "label": "按档位过滤",
                      "category": "按主分类过滤", "limit": "一页最多返回条数（≤20）",
                      "offset": "分页起点（上一页回程的 next_offset）"},
    "get_paper": {"arxiv_id": "论文 arXiv 编号"},
    "get_activity": {"since_seq": "只回此序号之后的事件", "actor": "按操作者过滤",
                     "op": "按操作类型过滤", "days": "背景统计的回溯天数",
                     "limit": "事件最多返回条数"},
    "review_status": {"date": "日期 ISO 格式（省略=今天）"},
    "paper_metrics": {"arxiv_id": "论文 arXiv 编号"},
    "get_references": {"arxiv_id": "论文 arXiv 编号", "limit": "最多返回条数（≤100）",
                       "sort_by_citations": "是否按引用数降序"},
    "get_citations": {"arxiv_id": "论文 arXiv 编号", "limit": "最多返回条数（≤100）",
                      "sort_by_citations": "是否按引用数降序"},
    "undo": {"seq": "要撤销的事件序号（0=最近一条可逆）", "reason": "一句话中文说明撤销原因"},
    "fetch_papers": {"days": "回溯天数", "reason": "一句话中文说明本次抓取目的"},
    "fetch_paper_by_id": {"arxiv_id": "论文 arXiv 编号（形如 1706.03762，可带 vN，勿带 URL）",
                          "reason": "一句话中文说明入库原因"},
    "prepare_review": {"date": "日期 ISO 格式（省略=今天）",
                       "requeue": "适用：池子被上轮消费光、想原班人马再审——把近 lookback 内 "
                                  "archived/in_briefing 拉回 new 重新出题（可 undo 回退）",
                       "stage": "full（默认，全文摘要直接评）| brief（W5 粗筛：标题+300字短摘，"
                                "选完 shortlist 再用 stage=full+arxiv_ids 拉全文）",
                       "arxiv_ids": "逗号分隔的 shortlist（配合 stage=full 使用：回执只装这些篇目的全文摘要；"
                                    "省略=全量回执）。**显式点名的篇目不受评审下限过滤**（N12："
                                    "显式意图优先于启发式预筛）",
                       "reason": "一句话中文说明目的"},
    "submit_review": {"reviews": "评审列表 [{arxiv_id,score,label,reason,…}]",
                      "date": "评审对应日期 ISO 格式（省略=今天）",
                      "reason": "一句话中文说明目的"},
    "list_briefings": {"limit": "最多返回条数（1-60，默认 14）"},
    "feed_generate": {"limit": "本次端多少篇（1-200，默认 25；AI 按语境自定）",
                      "days": "候选窗口：近 N 天入库论文（默认 14）",
                      "mix": "口味预设 auto|strict|explorer（explorer=想看点野的）",
                      "quotas": "显式四道配比 csv，如 '40,25,10,25'（覆盖 mix；探索地板 10% 压不穿）",
                      "seen_days": "近 N 天有过信号的篇目不重喂（默认 7，0=不排）",
                      "offset": "换一屏的游标：跳过装配结果的前 N 篇（默认 0）。用户说'刷新/换一屏'⇒ offset=已端过的篇数；池子见底时回执 notes 会说"},
    "publish_feed": {"limit": "本篇数（同 feed_generate）", "days": "候选窗口天数",
                     "mix": "auto|strict|explorer", "offset": "换页游标：续用上次回执 meta.next_offset",
                     "quotas": "显式四道配比 csv（可选）", "seen_days": "信号排重窗口（默认 7）",
                     "reason": "一句话中文说明这期为何而发"},
    "write_summary": {"arxiv_id": "论文 arXiv 编号（需已入库）",
                      "tldr": "一句话总括（中文，卡面高亮位）", "problem": "问题",
                      "method": "方法", "results": "结论", "novelty": "贡献",
                      "keywords": "关键词，逗号分隔",
                      "score": "0-1，与 label 成对；省略/-1=不评分",
                      "label": "must_read|worth|skip，与 score 成对",
                      "reason": "推荐理由（也进审计）"},
    "get_profile": {"top": "每维返回条数（1-50，默认 12）",
                    "half_life_days": "衰减半衰期（天，默认 30）：旧兴趣按指数淡出"},
    "reset_profile": {"kind": "只清某一维 category|term|author；空=全部",
                      "reason": "一句话中文说明重置原因"},
    "record_signal": {"arxiv_id": "论文 arXiv 编号（需已入库）",
                      "signal": "view|outbound|download|star|read|skip|uninterested",
                      "reason": "一句话中文说明信号来源"},
    "delete_briefing": {"date": "简报日期 ISO 格式（必填；先 list_briefings 确认）",
                        "reason": "一句话中文说明删除原因"},
    "finalize_briefing": {"date": "日期 ISO 格式（省略=今天）", "force": "已定稿时是否强制重跑",
                          "max_items": "本次定稿最多入选篇数（1-200；省略=按配置）——显式篇数胜配置（N12 同族）",
                          "reason": "一句话中文说明目的"},
    "run_pipeline": {"date": "日期 ISO 格式（省略=今天）", "force": "已存在时是否强制重跑",
                     "reason": "一句话中文说明目的"},
    "add_topic": {"name": "主题名", "keywords": "关键词，逗号分隔",
                  "categories": "arXiv 分类白名单，逗号分隔", "description": "主题描述",
                  "exclude_keywords": "排除词，逗号分隔", "quota": "每主题配额",
                  "threshold": "入选评分阈值", "reason": "一句话中文说明新增原因"},
    "update_topic": {"name": "要改的主题名（必填）",
                     "description": "新描述；省略=不动",
                     "keywords": "新关键词，逗号分隔；省略=不动（替换而非追加）",
                     "categories": "新分类白名单，逗号分隔；省略=不动",
                     "exclude_keywords": "新排除词，逗号分隔；省略=不动",
                     "authors": "关注作者，逗号分隔（命中者基线分加成）；省略=不动",
                     "quota": "新配额；负数=不动", "threshold": "新阈值 0-1；负数=不动",
                     "reason": "一句话中文说明改的原因"},
    "set_topic_enabled": {"name": "主题名", "enabled": "True 启用 / False 停用",
                          "reason": "一句话中文说明原因"},
    "mark_read": {"arxiv_id": "论文 arXiv 编号；可逗号分隔多篇（逐篇处理，坏 id 不伤其余）",
                  "read": "True 已读 / False 未读",
                  "reason": "一句话中文说明原因"},
    "star_paper": {"arxiv_id": "论文 arXiv 编号", "reason": "一句话中文说明原因"},
    "skip_paper": {"arxiv_id": "论文 arXiv 编号", "reason": "一句话中文说明原因"},
    "add_note": {"arxiv_id": "论文 arXiv 编号", "content": "笔记正文",
                 "reason": "一句话中文说明原因"},
}


def _split(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _edge(item: dict, key: str) -> dict:
    """把 S2 的 references/citations 边（{citedPaper|citingPaper, intents, isInfluential}）规范化成浓缩结构。"""
    p = item.get(key) or {}
    ext = p.get("externalIds") or {}
    return {
        "arxiv_id": ext.get("ArXiv"),
        "s2_id": p.get("paperId"),
        "title": p.get("title"),
        "year": p.get("year"),
        "venue": p.get("venue"),
        "citation_count": p.get("citationCount"),
        "influential": bool(item.get("isInfluential")),
        "intents": item.get("intents") or [],
        "tldr": (p.get("tldr") or {}).get("text"),
    }


def _parse_date(date_str: str):
    return date_cls.fromisoformat(date_str) if date_str else None


def build_registry(container) -> Registry:
    """构建能力注册表（工具 = container 里 service 的薄封装闭包）。"""
    reg = Registry()
    repo = container.repo
    pipeline = container.pipeline
    retrieval = container.retrieval
    settings = container.settings

    # ============================================================== 只读面
    @reg.tool(name="list_topics", kind="read",
              description="列出研究主题（名称/关键词/分类白名单/关注作者/配额/阈值/启用态）。")
    def list_topics() -> dict:
        return ok(topics=[
            {
                "name": t.name, "description": t.description,
                "keywords": list(t.keywords or []), "categories": list(t.categories or []),
                "exclude_keywords": list(t.exclude_keywords or []),
                "authors": list(t.authors or []),
                "quota": t.quota, "threshold": t.threshold, "enabled": t.enabled,
            }
            for t in settings.topics
        ])

    @reg.tool(name="get_digest", kind="read",
              description="某日简报全文(Markdown)+统计。date 空=今天；full=False 只回统计与条目。")
    def get_digest(date: str = "", full: bool = True) -> dict:
        target = date or datetime.now().date().isoformat()
        briefing = repo.briefing_for_date(target)
        if briefing is None:
            return ok(date=target, exists=False,
                      hint="当天没有简报；先 fetch_papers + prepare/submit/finalize_review，或 run_pipeline")
        stats = briefing.stats or {}
        out = ok(date=target, exists=True, title=briefing.title,
                 ai_enabled=briefing.ai_enabled, stats=stats.get("stats", {}),
                 items=stats.get("items", []),
                 archived_count=len(stats.get("archived", [])))
        if full:
            out["markdown"] = briefing.markdown
        return out

    @reg.tool(name="search_papers", kind="read",
              description="论文库检索(FTS5:标题/摘要/TL;DR)。query 空则按时间倒序列近期论文。分页：offset 从 0 起，"
                          "回程带 next_offset（非空则可继续取）。")
    def search_papers(query: str = "", label: str = "", category: str = "",
                     limit: int = 20, offset: int = 0) -> dict:
        # 页大小对齐体积闸 _LIST_KEEP=20：一次一页、offset 前进，避免“返回条数≠count”的不一致。
        lim = max(1, min(int(limit), 20))
        off = max(0, int(offset))
        papers = retrieval.search(
            query, label=label or None, primary_category=category or None,
            limit=lim, offset=off,
        )
        # N4：满页⇒给 next_offset（可执行的“AI 路”翻页，替代“看 Web 面板”那种人路提示）。
        next_offset = off + len(papers) if len(papers) == lim else None
        return ok(count=len(papers), offset=off, next_offset=next_offset, papers=[
            {
                "arxiv_id": p.arxiv_id, "title": p.title,
                "primary_category": p.primary_category,
                "published_at": p.published_at.isoformat() if p.published_at else None,
                "status": p.status,
                "score": p.scores[-1].score if p.scores else None,
                "label": p.scores[-1].label if p.scores else None,
                "star": bool(p.reading and p.reading.star),
            }
            for p in papers
        ])

    @reg.tool(name="get_paper", kind="read",
              description="论文详情：原文摘要 + 最新 AI 总结 + 打分历史 + 笔记 + 阅读态 + arXiv 直链。")
    def get_paper(arxiv_id: str) -> dict:
        detail = retrieval.detail(arxiv_id)
        if detail is None:
            return err("not_found", f"找不到论文 {arxiv_id}",
                       hint="先用 search_papers 搜到正确 arxiv_id")
        p, s = detail["paper"], detail["summary"]
        return ok(
            paper={
                "arxiv_id": p.arxiv_id, "title": p.title,
                "authors": list(p.authors or []), "categories": list(p.categories or []),
                "primary_category": p.primary_category,
                "published_at": p.published_at.isoformat() if p.published_at else None,
                "abs_url": p.abs_url, "pdf_url": p.pdf_url, "status": p.status,
                "abstract": p.abstract,
            },
            summary=(
                {"tldr": s.tldr, "problem": s.problem, "method": s.method,
                 "results": s.results, "novelty": s.novelty,
                 "keywords": list(s.keywords or []), "model": s.model}
                if s else None
            ),
            scores=[
                {"score": sc.score, "label": sc.label, "reason": sc.reason,
                 "model": sc.model, "run_id": sc.run_id}
                for sc in detail["scores"]
            ],
            notes=[n.content for n in detail["notes"]],
            reading=(
                {"read": detail["reading"].read, "star": detail["reading"].star,
                 "marked_skip": detail["reading"].marked_skip}
                if detail["reading"] else None
            ),
        )

    @reg.tool(name="get_activity", kind="read",
              description="归因面：①事件总线 diff-since-seq（actor/op 过滤，append-only）②近期运行/AI调用/简报统计。")
    def get_activity(since_seq: int = 0, actor: str = "", op: str = "",
                     days: int = 7, limit: int = 50) -> dict:
        events = repo.events_since(since_seq=since_seq, actor=actor, op=op, limit=limit)
        since = datetime.utcnow() - timedelta(days=max(1, int(days)))
        calls = repo.ai_calls_since(since, limit=50)
        briefings = repo.briefings(limit=14)
        return ok(
            events=events["events"], events_last_seq=events["last_seq"],
            events_count=events["count"],
            events_filters={"since_seq": since_seq, "actor": actor, "op": op},
            counts_by_status=repo.counts_by_status(),
            recent_briefings=[
                {"date": b.date, "title": b.title, "status": b.status, "ai_enabled": b.ai_enabled}
                for b in briefings
            ],
            ai_calls=[
                {"ts": c.ts.isoformat(), "port": c.port, "purpose": c.purpose,
                 "model": c.model, "latency_ms": c.latency_ms, "tokens": c.tokens,
                 "ok": c.ok, "error": c.error}
                for c in calls
            ],
        )

    @reg.tool(name="review_status", kind="read",
              description="看某天评审进度（候选数/已评审数/状态）。")
    def review_status(date: str = "") -> dict:
        return pipeline.review_status(date or datetime.now().date().isoformat())

    # ============================================================== 写入 / 运行面
    @reg.tool(name="undo", kind="write", reversible=True,
              description="撤销一条可逆写入（seq=0=最近一条可逆事件）。入库/定稿不可逆，会明说。")
    def undo(seq: int = 0, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        return repo.undo(int(seq), actor=actor, reason=reason)

    @reg.tool(name="fetch_papers", kind="write",
              description="按主题抓取 arXiv 最近 N 天提交的新论文入库（遵守 3s 限速，可能较慢）。")
    def fetch_papers(days: int = 3, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        client = ArxivClient(cache_dir=settings.cache_dir / "arxiv")
        try:
            papers = client.fetch_candidates(
                topics=settings.topics, categories=settings.arxiv_categories,
                since=datetime.utcnow() - timedelta(days=max(1, int(days))),
                global_fallback=settings.fetch_global_fallback,
            )
        finally:
            client.close()
        result = repo.upsert_papers(
            papers, actor=actor, reason=reason or f"抓取最近 {days} 天论文"
        )
        return ok(fetched=len(papers), new=result["new"], updated=result["updated"],
                  hint="接着 prepare_review 生成待评审候选")

    @reg.tool(name="fetch_paper_by_id", kind="write",
              description="按 arXiv id 把单篇拉进入库（对话里'这篇加进来'）；幂等，已在库回 cached。"
                          "拉入后即可 read_paper/add_note/mark_read；站内下载走 /papers/{id}/pdf 跳转。")
    def fetch_paper_by_id(arxiv_id: str, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        client = ArxivClient(cache_dir=settings.cache_dir / "arxiv")
        try:
            papers = client.fetch_by_ids([arxiv_id])
        finally:
            client.close()
        if not papers:
            return err("not_found", f"arXiv 上没找到 {arxiv_id}",
                       hint="核对 id（形如 1706.03762，可带 vN；勿贴 URL 整串）")
        result = repo.upsert_papers(papers, actor=actor,
                                    reason=reason or f"按 id 入库 {arxiv_id}")
        cached = int(result.get("new", 0)) == 0
        return ok(arxiv_id=papers[0].arxiv_id, title=papers[0].title,
                  new=result.get("new"), updated=result.get("updated"), cached=cached,
                  hint="已在库（幂等）" if cached
                  else "已入库，可 read_paper/add_note/站内直下 PDF")

    @reg.tool(name="write_summary", kind="write", reversible=True,
              description="单篇补卡：对任意已入库论文直接产一张日报级卡（总结五段 + 可选评分/档位），"
                          "/feed 卡与详情页、read_paper 立即自动复用。对话里'给这篇补张卡'就用它；"
                          "score 与 label 成对出现（不猜半张卡）。")
    def write_summary(arxiv_id: str, tldr: str = "", problem: str = "",
                      method: str = "", results: str = "", novelty: str = "",
                      keywords: str = "", score: float = -1.0, label: str = "",
                      reason: str = "", actor: str = ACTOR_DEFAULT) -> dict:
        if repo.get_paper(arxiv_id) is None:
            return err("not_found", f"库里没有 {arxiv_id}",
                       hint="先 fetch_paper_by_id 拉进入库")
        summary = {"tldr": tldr, "problem": problem, "method": method,
                   "results": results, "novelty": novelty, "keywords": _split(keywords)}
        sc = None
        if score >= 0 or label:
            if not (0.0 <= score <= 1.0) or label not in ("must_read", "worth", "skip"):
                return err("bad_params",
                           f"score/label 须成对合规：score∈[0,1]（收到 {score}）、"
                           "label∈must_read|worth|skip（收到 {label!r}）",
                           hint="只写总结不打分完全合法——但别送半张卡")
            sc = {"score": float(score), "label": label,
                  "reason": reason or "AI 补卡",
                  "model": "dsh-review" if actor == "ai" else "human-card"}
        if not any(summary.values()) and sc is None:
            return err("no_fields", "总结五段与评分全空——这张卡没内容可写",
                       hint="至少一项：tldr/problem/method/results/novelty/keywords 或 score+label")
        out = repo.write_summary(arxiv_id, summary=summary, score=sc, actor=actor,
                                 reason=reason)
        if out is None:
            return err("not_found", f"库里没有 {arxiv_id}（竞态：刚才还在）")
        return ok(arxiv_id=arxiv_id, run_id=out["run_id"],
                  wrote_summary=True, wrote_score=sc is not None,
                  hint="/feed 卡与详情页的摘要块即刻复用本卡；写错了 undo_change 可撤整张")

    @reg.tool(name="prepare_review", kind="write",
              description="阶段1：取过规则后的候选清单（含主题画像+摘要截断+基线分），等外部评审。"
                          "同一天想再跑一遍评审可 requeue=True（把近 lookback 内 archived/in_briefing "
                          "拉回 new 再审；走可逆事件，undo 一键回退，旧简报行仍在库中）。")
    def prepare_review(date: str = "", requeue: bool = False, stage: str = "full",
                       arxiv_ids: str = "",
                       actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        if stage not in ("full", "brief"):
            return err("bad_params", f"stage 只能是 full|brief，收到 {stage!r}",
                       hint="两阶段评审：先 stage='brief' 粗筛，再 stage='full'+arxiv_ids 精评")
        prepared = pipeline.prepare_review(
            _parse_date(date), actor=actor, reason=reason or "外部请求评审候选",
            requeue=requeue, stage=stage,
            arxiv_ids=tuple(x.strip() for x in arxiv_ids.replace("，", ",").split(",") if x.strip()),
        )
        payload = prepared.payload
        diag = payload.get("_empty_diagnosis")
        if diag:
            # 空池：不再静默 ok:true+candidates:[]；把真相+出路回成标准 err 信封。
            return err(str(diag.get("kind") or "empty_pool"),
                       str(diag.get("reason") or "候选池为空"),
                       hint=str(diag.get("hint") or ""),
                       suggest=list(diag.get("suggests") or []))
        return ok(**payload)

    @reg.tool(name="get_profile", kind="read",
              description="读兴趣画像：arXiv 分类/词/作者三维权重 top + 分类熵（防茧房哨兵）。"
                          "行为信号驱动，YAML 主题只是先验种子。")
    def get_profile(top: int = 12, half_life_days: float = 30.0) -> dict:
        repo.profile_seed_if_empty(settings.topics)
        view = repo.profile_view(top=max(1, min(int(top), 50)),
                                 half_life_days=max(0.001, float(half_life_days)))
        return ok(**view, note="画像由行为信号驱动；熵过低=兴趣收窄，feed 会自动加倍探索道；"
                                "重置仅人类侧（reset_profile）")

    @reg.tool(name="feed_generate", kind="read",
              description="生成兴趣推荐流（四道召回：主兴趣/邻接桥/热点作者/探索，带道属与 why，"
                          "确定性可复算、0 token）。limit/mix/quotas 由调用者按语境自由定——显式意图胜默认。")
    def feed_generate(limit: int = 25, days: int = 14, mix: str = "auto",
                      quotas: str = "", seen_days: int = 7, offset: int = 0,
                      actor: str = ACTOR_DEFAULT) -> dict:
        from ..domain.feed import allocate, maybe_entropy_boost, resolve_quotas
        from ..domain.profile import paper_features, score_paper
        from ..infra import arxiv_taxonomy as tax
        if not 1 <= int(limit) <= 200:
            return err("bad_params", f"limit={limit} 越界（1-200）",
                       hint="一次 20~40 是合适的刷屏量；更大窗口建议分批")
        if int(offset) < 0:
            return err("bad_params", f"offset={offset} 不能为负",
                       hint="换一屏用上次回执的 meta.next_offset（或直接改 days/mix/seen_days 重配口味）")
        repo.profile_seed_if_empty(settings.topics)
        view = repo.profile_view()
        weights = repo.profile_weights_map()
        if not weights:
            return err("empty_profile", "画像无任何权重（无信号且无主题可播）",
                       hint="先 fetch_papers 让库里有货并产生信号，或 record_signal 声明兴趣")
        rows = repo.feed_candidates(days=max(1, int(days)))
        if not rows:
            return ok(feed=[], count=0, meta={"days": days, "mix": mix, "notes": [],
                                              "explore_share_pct": 0,
                                              "category_entropy": view["category_entropy"]},
                      hint=f"近 {days} 天库内无新论文：先 fetch_papers(days={max(1, int(days))}) 再刷")
        seen = repo.feed_seen_ids(days=max(0, int(seen_days)))
        top_pos = [k for k, w, _h in view["top"]["category"] if w > 0]
        top_set = set(top_pos)
        adj: set[str] = set()
        for c in list(top_set)[:8]:
            adj.update(tax.siblings_of(c))
            bridges = set(tax.bridge_groups(c))
            adj.update(x for x in tax.KNOWN if tax.group_of(x) in bridges)
        adj -= top_set
        top_auth = {k for k, w, _h in view["top"]["author"] if w > 0}
        buckets: dict[str, list[dict]] = {"primary": [], "adjacent": [], "hot": [], "explore": []}
        for p in rows:
            if p.arxiv_id in seen:
                continue
            feat = paper_features(list(p.categories or []), p.primary_category,
                                  p.title or "", p.abstract or "", list(p.authors or []))
            sc, why = score_paper(feat, weights)
            pr = feat["primary"]
            if pr in top_set:
                lane = "primary"
            elif any(c in adj for c in feat["categories"]):
                lane = "adjacent"
            elif top_auth & set(feat["authors"]):
                lane = "hot"
            else:
                lane = "explore"
            if not why:   # 宁窄勿玄的反面是“宁窄勿无由”：道属本身就是可读理由
                why = [{
                    "primary": f"主兴趣：{pr} 在画像高分区",
                    "adjacent": f"邻接桥：{pr or '未标分类'} 与画像分类相邻（扩边召回）",
                    "hot": "热点：关注作者的新论文",
                    "explore": f"探索位：{pr or '未标分类'} 在画像外——扩边界是默认目标（信条 7）",
                }[lane]]
            buckets[lane].append({
                "arxiv_id": p.arxiv_id, "title": p.title, "primary_category": pr,
                "categories": feat["categories"][:4], "authors": feat["authors"][:5],
                "published": p.published_at.date().isoformat() if p.published_at else "",
                "score": round(sc, 4), "why": why, "lane": lane})
        for lst in buckets.values():
            lst.sort(key=lambda x: (-x["score"], x["arxiv_id"]))    # 平分时按 id，确定性
        q, note_q = resolve_quotas(mix, quotas)
        boost = ""
        if not (quotas or "").strip() and (mix or "auto") == "auto":
            q, boost = maybe_entropy_boost(q, view["category_entropy"])
        picked = allocate(buckets, limit=int(limit) + int(offset), quotas=q)
        screen = picked[int(offset):int(offset) + int(limit)]
        n_exp = sum(1 for x in screen if x["lane"] == "explore")
        meta = {"mix": (mix or "auto"), "quotas": list(q),
                "explore_share_pct": round(100.0 * n_exp / max(1, len(screen)), 1),
                "category_entropy": view["category_entropy"], "seen_excluded": len(seen),
                "days": days, "pool": sum(len(v) for v in buckets.values()),
                "offset": int(offset), "next_offset": int(offset) + len(screen),
                "pool_left": max(0, sum(len(v) for v in buckets.values()) - int(offset) - len(screen)),
                "notes": [x for x in (note_q, boost) if x]}
        if int(offset) and not screen:
            meta["notes"].append(
                f"offset={offset} 已越过池底（pool={meta['pool']}）：回第一屏用 offset=0，"
                "或 fetch_papers 补货/加大 days/清 seen_days")
        repo.record_op("feed_generate", actor=actor,
                       reason=f"刷流 offset={offset} limit={limit} mix={mix or 'auto'}",
                       after={"count": len(screen), "offset": int(offset),
                              "pool": meta["pool"], "mix": meta["mix"],
                              "lanes": {lane: sum(1 for x in screen if x["lane"] == lane)
                                        for lane in ("primary", "adjacent", "hot", "explore")},
                              "ids": [x["arxiv_id"] for x in screen[:12]]})
        return ok(feed=screen, count=len(screen), meta=meta)

    @reg.tool(name="publish_feed", kind="write", reversible=True,
              description="把显式参数装配的一期发布为 feed 最新期（写库+审计）：/feed 面板即显示这期，"
                          "**不现场重算**。用户说“20 条 60 天/换一页/野一点”都是再调一次本工具；"
                          "换页 offset 直接续用上次回执 meta.next_offset（零重叠零空洞）。")
    def publish_feed(limit: int = 25, days: int = 14, mix: str = "auto",
                     offset: int = 0, quotas: str = "", seen_days: int = 7,
                     actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        gen = feed_generate(limit=limit, days=days, mix=mix, quotas=quotas,
                            seen_days=seen_days, offset=offset, actor=actor)
        if not gen.get("ok"):
            return gen
        params = {"limit": int(limit), "days": int(days), "mix": (mix or "auto"),
                  "offset": int(offset), "quotas": (quotas or ""),
                  "seen_days": int(seen_days)}
        items = [{k: e[k] for k in ("arxiv_id", "title", "primary_category", "categories",
                                    "authors", "published", "score", "why", "lane")}
                 for e in gen["feed"]]
        issue_id = repo.save_feed_issue(params=params, items=items, actor=actor,
                                        reason=reason or "dsh 发布 feed 一期")
        return ok(issue_id=issue_id, count=len(items), params=params,
                  meta=gen["meta"],
                  hint=f"第 {issue_id} 期已上 /feed 面板；换页就接着调 "
                       f"publish_feed(offset={gen['meta']['next_offset']}, …)，参数全部显式、面板不自行重算。")

    @reg.tool(name="reset_profile", kind="write", reversible=True,
              description="清空/重置兴趣画像（**人类专属**：不投影给 AI，防自改锚点）。"
                          "快照留痕，undo_change 可还原。")
    def reset_profile(kind: str = "", actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        if kind and kind not in ("category", "term", "author"):
            return err("bad_params", f"kind 只能是 category|term|author|空(全部)，收到 {kind!r}")
        return repo.profile_reset(kind=kind, actor=actor, reason=reason)

    @reg.tool(name="record_signal", kind="write",
              description="记一个兴趣信号：对话里用户说'我下了/看了/不感兴趣'就用它声明，"
                          "与站内实测信号（跳转路由自动记）**同表同权**；需论文已入库。")
    def record_signal(arxiv_id: str, signal: str, actor: str = ACTOR_DEFAULT,
                      reason: str = "") -> dict:
        allowed = [k for k in repo.SIGNAL_WEIGHTS if k != "seed"]
        if signal not in repo.SIGNAL_WEIGHTS or signal == "seed":
            return err("bad_params", f"未知信号 {signal!r}",
                       hint=f"可选：{'/'.join(allowed)}", suggest=allowed)
        if repo.get_paper(arxiv_id) is None:
            return err("not_found", f"库里没有 {arxiv_id}，先 fetch_paper_by_id 拉入再记信号",
                       hint="信号的特征来自论文自分类/标题/作者，需先入库")
        repo.profile_seed_if_empty(settings.topics)
        repo.record_signal(arxiv_id, signal, source="declared", actor=actor,
                           reason=reason or f"AI 声明信号 {signal}")
        return ok(arxiv_id=arxiv_id, signal=signal, source="declared",
                  hint="画像已增量更新；get_profile 可验证方向")

    @reg.tool(name="submit_review", kind="write",
              description="阶段2：提交对候选的评审。reviews=[{arxiv_id,score,label,reason,tags?,summary?}]；"
                          "label 取 must_read/worth/skip。date 省略=今天（与 prepare/finalize 对齐）。")
    def submit_review(reviews: list, date: str = "", actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        target = date or datetime.now().date().isoformat()
        return pipeline.submit_review(target, list(reviews or []))

    @reg.tool(name="finalize_briefing", kind="write",
              description="阶段3：用已提交评审（缺的用基线分）做筛选、精读、生成简报并落库。")
    def finalize_briefing(date: str = "", force: bool = False, max_items: int = 0,
                          actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        from ..domain.pipeline import pipeline_config_override
        if max_items and not 1 <= int(max_items) <= 200:
            return err("bad_params", f"max_items={max_items} 越界（1-200；0=按配置）")
        if max_items:
            # N12 同族：显式篇数 > 配置（gate/yaml），只活在本次调用的线程局部覆盖里
            with pipeline_config_override(scoring=settings.scoring.model_copy(
                    update={"max_papers": int(max_items)})):
                result = pipeline.finalize_review(
                    _parse_date(date), force=force, actor=actor, reason=reason or "定稿简报")
        else:
            result = pipeline.finalize_review(
                _parse_date(date), force=force, actor=actor, reason=reason or "定稿简报")
        if result.error:
            return err("finalize_failed", result.error)
        return ok(date=result.date, run_id=result.run_id, fetched=result.fetched,
                  after_rules=result.after_rules, selected=result.selected,
                  reused=result.reused, briefing_id=result.briefing_id,
                  degraded=result.degraded,
                  max_items_applied=int(max_items) if max_items else None)

    @reg.tool(name="run_pipeline", kind="write",
              description="一键全流程（无外部评审）：候选→规则→程序化AI档（未配key时heuristic）→简报。")
    def run_pipeline(date: str = "", force: bool = False,
                     actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        result = pipeline.run(
            _parse_date(date), force=force, actor=actor, reason=reason or "一键跑流水线"
        )
        if result.error:
            return err("pipeline_failed", result.error)
        return ok(date=result.date, run_id=result.run_id, fetched=result.fetched,
                  after_rules=result.after_rules, selected=result.selected,
                  reused=result.reused, degraded=result.degraded)

    @reg.tool(name="list_briefings", kind="read",
              description="列出已归档的简报（日期/run_id/入选篇数/是否 AI/状态），按日期倒序。"
                          "与 read_digest 分工：本工具给管理面（有哪些日报可删）；read_digest 给阅读面。")
    def list_briefings(limit: int = 14) -> dict:
        rows = repo.briefings(limit=max(1, min(int(limit), 60)))
        return ok(count=len(rows), briefings=[
            {"date": b.date, "run_id": b.run_id, "status": b.status,
             "selected": (len(b.stats.get("items", []))
                          if isinstance(b.stats, dict) else 0),
             "ai_enabled": bool(b.ai_enabled)}
            for b in rows
        ])

    @reg.tool(name="delete_briefing", kind="write", reversible=True,
              description="删除指定日期的简报（同日多版本一并删）。可撤销——事件存了 markdown+stats 快照，"
                          "undo_change(seq=0) 一键重建。Web 设置页的「删简报」按钮与此同源。")
    def delete_briefing(date: str, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        if not date:
            return err("bad_params", "date 必填",
                       hint="先 list_briefings 看有哪些日期")
        res = repo.delete_briefing(date, actor=actor,
                                   reason=reason or f"删除简报（{date}）")
        if res.get("already"):
            return err("no_briefing", f"{date} 没有简报可删",
                       hint="先 list_briefings 确认日期（拼写、年份）")
        return ok(date=date, deleted=res.get("deleted"),
                  note="可用 undo_change(seq=0) 撤销（事件回滚会重建简报行）")

    @reg.tool(name="add_topic", kind="write",
              description="新增研究主题（写回 config/settings.yaml，即时生效）。keywords/categories 用逗号分隔。")
    def add_topic(name: str, keywords: str = "", categories: str = "", description: str = "",
                  exclude_keywords: str = "", quota: int = 4, threshold: float = 0.6,
                  actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        if any(t.name == name for t in settings.topics):
            return err("duplicate", f"主题「{name}」已存在",
                       suggest=[t.name for t in settings.topics][:5])
        topic = TopicCfg(
            name=name, description=description, keywords=_split(keywords),
            categories=_split(categories), exclude_keywords=_split(exclude_keywords),
            quota=quota, threshold=threshold, enabled=True,
        )
        settings.topics = [*settings.topics, topic]
        save_settings(settings)
        repo.sync_topics(settings.topics, actor=actor, reason=reason or f"新增主题「{name}」")
        return ok(added=name, total_topics=len(settings.topics))

    @reg.tool(name="update_topic", kind="write",
              description="更新既有主题（W7，AI 自助调优闭环）：只改传入的字段，省略=不动；"
                          "列表字段为**替换**语义、逗号分隔；暂不支持清空列表。"
                          "启用/停用请用 set_topic_enabled（职责不重叠）。")
    def update_topic(name: str, description: str = "", keywords: str = "",
                     categories: str = "", exclude_keywords: str = "", authors: str = "",
                     quota: int = -1, threshold: float = -1.0,
                     actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        idx = next((i for i, t in enumerate(settings.topics) if t.name == name), None)
        if idx is None:
            return err("unknown_topic", f"没有主题「{name}」",
                       hint="update_topic 只能改已有主题；新增用 add_topic",
                       suggest=[t.name for t in settings.topics][:8])
        topic = settings.topics[idx]
        changed: dict[str, object] = {}
        if description:
            topic.description = description
            changed["description"] = description
        for fld, raw in (("keywords", keywords), ("categories", categories),
                         ("exclude_keywords", exclude_keywords), ("authors", authors)):
            if raw:
                setattr(topic, fld, _split(raw))
                changed[fld] = _split(raw)
        if quota >= 1:
            topic.quota = int(quota)
            changed["quota"] = topic.quota
        if 0.0 <= threshold <= 1.0:
            topic.threshold = float(threshold)
            changed["threshold"] = topic.threshold
        if not changed:
            return err("no_fields", "未传任何要改的字段（全部省略）",
                       hint="至少传一个：keywords/categories/exclude_keywords/"
                            "authors/quota/threshold/description")
        save_settings(settings)
        repo.sync_topics(settings.topics, actor=actor,
                         reason=reason or f"更新主题「{name}」")
        return ok(topic=name, changed=sorted(changed), total_topics=len(settings.topics),
                  hint="已写回配置事实源并同步打分镜像；authors 命中作者的论文基线分会获得加成")

    @reg.tool(name="set_topic_enabled", kind="write",
              description="启用/停用某主题（写回 YAML）。")
    def set_topic_enabled(name: str, enabled: bool, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        for t in settings.topics:
            if t.name == name:
                t.enabled = bool(enabled)
                save_settings(settings)
                repo.sync_topics(settings.topics, actor=actor,
                                 reason=reason or f"{'启用' if enabled else '停用'}主题「{name}」")
                return ok(topic=name, enabled=bool(enabled))
        return err("unknown_topic", f"没有主题「{name}」",
                   suggest=[t.name for t in settings.topics][:5])

    # ============================================================== 阅读态（写）
    def _paper_or_err(arxiv_id: str):
        paper = repo.get_paper(arxiv_id)
        if paper is None:
            return None, err("not_found", f"找不到论文 {arxiv_id}",
                             hint="先用 search_papers 搜到正确 arxiv_id")
        return paper, None

    @reg.tool(name="mark_read", kind="write", reversible=True,
              description="标记论文已读/未读。")
    def mark_read(arxiv_id: str, read: bool = True, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        # W9 批量：逗号分隔多篇（per-item 纪律，坏 id 进 rejected 不伤其余）；
        # 单 id 保持旧返回形状，不碎已依赖它的调用方/判据。
        ids = [x.strip() for x in str(arxiv_id).replace("，", ",").split(",") if x.strip()]
        if not ids:
            return err("bad_params", "arxiv_id 为空",
                       hint='形如 "1706.03762"，多篇逗号分隔')
        if len(ids) == 1:
            paper, e = _paper_or_err(ids[0])
            if e:
                return e
            repo.set_read(paper, read=read, actor=actor, reason=reason)
            return ok(arxiv_id=ids[0], read=read)
        updated: list[str] = []
        rejected: list[dict] = []
        for one in ids:
            paper = repo.get_paper(one)
            if paper is None:
                rejected.append({"arxiv_id": one, "why": "库里没有这篇"})
                continue
            repo.set_read(paper, read=read, actor=actor,
                          reason=reason or f"批量标记{'已读' if read else '未读'}")
            updated.append(one)
        if not updated:
            return err("not_found", "所有 arxiv_id 都不在库中",
                       hint="先 search_papers 确认，或 fetch_paper_by_id 拉入",
                       suggest=[x["arxiv_id"] for x in rejected])
        return ok(arxiv_ids=updated, read=read, rejected=rejected,
                  hint=f"{len(updated)} 篇已{'标为已读' if read else '标为未读'}"
                       + (f"；{len(rejected)} 篇被拒" if rejected else ""))

    @reg.tool(name="star_paper", kind="write", reversible=True,
              description="收藏/取消收藏论文。")
    def star_paper(arxiv_id: str, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        # W9 批量：逗号分隔多篇逐篇 toggle；单 id 保持旧形状。注意 toggle 语义：
        # 批量时每篇各自翻面，不保证同态（回执逐篇给 star 值）。
        ids = [x.strip() for x in str(arxiv_id).replace("，", ",").split(",") if x.strip()]
        if not ids:
            return err("bad_params", "arxiv_id 为空", hint='形如 "1706.03762"，多篇逗号分隔')
        if len(ids) == 1:
            paper, e = _paper_or_err(ids[0])
            if e:
                return e
            return ok(arxiv_id=ids[0], star=repo.toggle_star(paper, actor=actor, reason=reason))
        results: list[dict] = []
        rejected: list[dict] = []
        for one in ids:
            paper = repo.get_paper(one)
            if paper is None:
                rejected.append({"arxiv_id": one, "why": "库里没有这篇"})
                continue
            results.append({"arxiv_id": one,
                            "star": repo.toggle_star(paper, actor=actor,
                                                     reason=reason or "批量收藏/取消")})
        if not results:
            return err("not_found", "所有 arxiv_id 都不在库中",
                       hint="先 search_papers 确认，或 fetch_paper_by_id 拉入",
                       suggest=[x["arxiv_id"] for x in rejected])
        return ok(starred=results, rejected=rejected,
                  hint=f"{len(results)} 篇已翻面" + (f"；{len(rejected)} 篇被拒" if rejected else ""))

    @reg.tool(name="skip_paper", kind="write", reversible=True,
              description="标记不感兴趣（同类下次过滤）。")
    def skip_paper(arxiv_id: str, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        paper, e = _paper_or_err(arxiv_id)
        if e:
            return e
        repo.set_marked_skip(paper, skip=True, actor=actor, reason=reason)
        return ok(arxiv_id=arxiv_id, marked_skip=True)

    @reg.tool(name="add_note", kind="write", reversible=True,
              description="给论文加笔记（调研沉淀）。")
    def add_note(arxiv_id: str, content: str, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        paper, e = _paper_or_err(arxiv_id)
        if e:
            return e
        note = repo.add_note(paper, content, actor=actor, reason=reason)
        return ok(arxiv_id=arxiv_id, note_id=note.id)

    # ============================ 引文分析（专项调查；数据源 Semantic Scholar）
    # arXiv 无引文数据；S2 提供引用数/参考/被引——“往前扒起源”的地基。
    def _scholar() -> SemanticScholarClient:
        return SemanticScholarClient(cache_dir=settings.cache_dir / "scholar")

    @reg.tool(name="paper_metrics", kind="read",
              description="一篇论文的影响力度量（Semantic Scholar）：被引数/参考数/高影响引用数/年份/venue/TLDR。用于判断分量与质量信号。")
    def paper_metrics(arxiv_id: str) -> dict:
        client = _scholar()
        try:
            p = client.paper(arxiv_ext_id(arxiv_id))
        finally:
            client.close()
        return ok(
            arxiv_id=arxiv_id, s2_id=p.get("paperId"), title=p.get("title"),
            year=p.get("year"), venue=p.get("venue"),
            citation_count=p.get("citationCount"), reference_count=p.get("referenceCount"),
            influential_citation_count=p.get("influentialCitationCount"),
            tldr=(p.get("tldr") or {}).get("text"),
            doi=(p.get("externalIds") or {}).get("DOI"),
        )

    @reg.tool(name="get_references", kind="read",
              description="取一篇论文引用的文献（往前追溯技术起源）。默认按被引论文引用数降序——排最前的即'起源/奠基'候选；含 intents(background/method/result)与是否高影响引用。可对结果递归再查以继续往前扒。")
    def get_references(arxiv_id: str, limit: int = 30, sort_by_citations: bool = True) -> dict:
        client = _scholar()
        try:
            items = client.references(arxiv_ext_id(arxiv_id), limit=min(int(limit), 100))
        finally:
            client.close()
        refs = [_edge(it, "citedPaper") for it in items]
        if sort_by_citations:
            refs.sort(key=lambda r: -(r.get("citation_count") or 0))
        return ok(
            arxiv_id=arxiv_id, count=len(refs),
            sort="citations_desc" if sort_by_citations else "api",
            references=refs,
            hint="引用数高且年份早的，通常是该技术的起源/奠基工作；再对它们递归 get_references 可继续往前扒",
        )

    @reg.tool(name="get_citations", kind="read",
              description="取引用了这篇论文的文献（往后看影响力扩散/后续工作）。可按引用数降序。")
    def get_citations(arxiv_id: str, limit: int = 30, sort_by_citations: bool = True) -> dict:
        client = _scholar()
        try:
            items = client.citations(arxiv_ext_id(arxiv_id), limit=min(int(limit), 100))
        finally:
            client.close()
        cits = [_edge(it, "citingPaper") for it in items]
        if sort_by_citations:
            cits.sort(key=lambda c: -(c.get("citation_count") or 0))
        return ok(arxiv_id=arxiv_id, count=len(cits), citations=cits)

    reg.attach_param_descriptions(PARAM_DESCRIPTIONS)   # N8：单一事实源装入 ToolSpec.params
    return reg
