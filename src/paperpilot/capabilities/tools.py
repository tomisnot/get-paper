"""能力工具集：把既有 service 薄封装成中性、自描述、可外部调用的能力。

**逻辑不重写**——全部委托 container 里的 repo/pipeline/retrieval/settings（单一事实源，
docs/PRINCIPLES.md §9）。读写分类与归因纪律见 docs/SPEC.md §3、§6：
- 只读能力（kind="read"）：外部可自由调用；
- 写入能力（kind="write"）：接受 actor/reason，落 append-only 事件总线，可 undo。
"""

from __future__ import annotations

import json
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path

from lxml import html as LH

from ..config import TopicCfg, save_settings
from ..infra.arxiv import ArxivClient
from ..infra.paperhtml import (
    Anchor,
    Block,
    LocateResult,
    PaperHtmlClient,
    extract_blocks,
    fetch_html,
    localize_and_clean,
    locate_quote,
    outline as blocks_outline,
    sha256_text,
    snippet,
)
from ..infra.scholar import SemanticScholarClient, arxiv_ext_id
from ..infra.shot import ShotError
from ..infra.shot import capture as shot_capture
from ..infra.shot import dump_attr as shot_dump_attr
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
    "sync_citations": {"arxiv_id": "在库论文 arXiv 编号", "limit": "取多少条引用（≤100，默认 40）",
                       "reason": "一句话中文说明为何落库"},
    "sync_cited_by": {"arxiv_id": "在库论文 arXiv 编号", "limit": "取多少条反引（≤100，默认 40）",
                      "reason": "一句话中文说明"},
    "tag_paper": {"arxiv_id": "在库论文 arXiv 编号",
                  "tag": "平台源头|理论源头|综述枢纽|实验谱系|下游扩散|动机（一论文一枚）",
                  "reason": "一句话中文说明为何这么标"},
    "tag_papers": {"items": "'arxiv:标签' 逗号分隔（如 1707.04344:平台源头,1208.1220:动机）",
                   "reason": "一句话中文说明这批为什么这么标"},
    "query_tags": {},
    "set_graph_view": {
        "name": "视图名（重名覆盖；默认视图＝/network 首屏）",
        "root": "单根聚焦的 arXiv 编号（空＝全库视角）",
        "depth": "从根 BFS 的半径（1-6）",
        "layout": "layer（拓扑分层）| timeline（年代列，看脉络）",
        "sides": "both（根居中，上游在上/下游在下）| upstream（只看它引的）| downstream（只看引用它的）",
        "in_lib_only": "True 时图上只放库内论文（缺的用 materialize_view 入库，别靠隐藏）",
        "color_by": "auto|kind|in_lib|weight|tag|group（auto＝有标签就用标签）",
        "group_by": "none|tag|group（泳道分组，出分组标题带）",
        "group_order": "泳道显示顺序，逗号分隔（如 '动机,平台源头,理论源头,综述枢纽,实验谱系,下游扩散'）",
        "label_mode": "auto|always|hover（auto＝布点数 ≤ label_auto_max 就常显）",
        "label_style": "title|id（拥挤时只显编号）",
        "label_max": "标签截断字数（6-60；全称留给 hover 卡）",
        "label_auto_max": "auto 模式的常显阈值：布点数 ≤ 此值就常显标签（0-400）",
        "badge": "True/False：节点角标显示分类前两字（文字，不只靠颜色）",
        "arrow_size": "箭头像素尺寸（6-40；userSpaceOnUse，画在节点圆外）",
        "max_nodes": "布点上限（4-400）",
        "max_edges": "画边上限（10-3000，按被引数采样）",
        "group_quota": "每组保底篇数（0-50，防高被引把少数派挤掉）",
        "sort_within": "weight（按库内同引）| year（按年份）| align（**按连接重心对齐**，"
                       "把有引用关系的点上下对齐、连线最短；与 rank 可同时用）",
        "size_by": "degree|weight|flat（节点大小依据）",
        "layer_gap": "层距/列距像素（40-400）",
        "node_gap": "同层节点间距像素（24-300）",
        "pin": "锚点 arXiv 编号（逗号分隔，永不截断）",
        "rank": "**AI 的显式优先级**（arXiv 编号按重要性排序，逗号分隔）——压过度数与分组，"
                "配 place=center 让骨干贴着中轴、连线最短",
        "place": "lane（按组聚簇成泳道）| center（按 rank 从行中心向两侧展开）",
        "layers": "**自定义分层**：'arxiv:层号' 逗号分隔（层号：负＝上游第几跳、0＝本体、正＝下游），"
                  "压过 BFS 跳数——按你分析出的逻辑关系分层；只认图里有边的 id",
        "mode": "auto＝机器替你捞一圈的**草稿**（供你读一眼再决定）| curated＝**你点名的清单**"
                "（layers 即内容：点名的才上图，机器不加不减、不算配额；写下的顺序即层内次序）",
        "title": "视图标题（页头那行话）",
        "group_map": "'arxiv:组名' 逗号分隔（自定义叙事分组，替代固定六色）",
        "group_colors": "'组名:#色值' 逗号分隔",
        "is_default": "True 时设为默认视图",
        "reason": "一句话中文说明这张图要表达什么"},
    "query_graph_views": {},
    "query_graph_views": {"name": "视图名（省略=列出全部；给了就把它整幅拉出来，含完整 spec）"},
    "delete_graph_view": {"name": "要删除的视图名", "reason": "一句话中文说明为什么删"},
    "fetch_paper_html": {"arxiv_id": "论文 arXiv 编号（需已入库）",
                         "force": "True 时即使已归档也重抓（换版本/重锚前用）",
                         "reason": "一句话中文说明为何归档"},
    "read_paper_outline": {"arxiv_id": "论文 arXiv 编号"},
    "search_library_text": {"q": "检索词（原文里的字样）",
                            "limit": "最多命中条数（默认 20，上限 50）"},
    "read_paper_text": {"arxiv_id": "论文 arXiv 编号",
                        "section": "只读某一节（给节标题包含的字符串，如 'Method'）",
                        "block": "只读某一块（给块 id，如 S3.p2）",
                        "offset": "从该块序号/字符偏移开始（省略=从头）",
                        "limit": "最多返回字符数（默认 6000，上限 20000）"},
    "search_paper_text": {"arxiv_id": "论文 arXiv 编号", "q": "检索词（原文里的字样）",
                          "limit": "最多命中条数（默认 8）"},
    "annotate_paper": {"arxiv_id": "论文 arXiv 编号",
                       "quote": "要被标注的**原文原句**（推荐；由后端解析成精确区间）",
                       "block": "块 id（quote 有歧义时用它指定；或整块标注时只给 block）",
                       "body": "批注正文（Markdown；可留空＝纯高亮）",
                       "color": "颜色名或色值（省略＝按 kind 取默认）",
                       "kind": "highlight（默认）| note（带批注气泡）| section（整节）| figure（整图/表）",
                       "reason": "一句话中文说明这条批注为什么"},
    "update_mark": {"mark_id": "批注 id", "body": "新的批注正文（省略=不动）",
                    "status": "active|resolved（省略=不动）", "color": "新颜色（省略=不动）",
                    "reason": "一句话中文说明改动原因"},
    "resolve_mark": {"mark_id": "批注 id", "reason": "一句话中文说明为何标为已解决"},
    "delete_mark": {"mark_id": "要删除的批注 id", "reason": "一句话中文说明为什么删"},
    "verify_marks": {"arxiv_id": "论文 arXiv 编号",
                     "since_id": "只看这个 id 之后的批注（省略=全部）"},
    "capture_paper_shot": {"arxiv_id": "论文 arXiv 编号",
                           "mark_id": "只拍某条批注附近（省略=整页全图）",
                           "width": "视口宽度像素（默认 1440）",
                           "reason": "一句话中文说明为何截图"},
    "read_paper_shots": {"arxiv_id": "论文 arXiv 编号", "limit": "最多回几条（默认 5）"},
    "set_default_view": {"name": "已发布的视图名", "reason": "一句话中文说明为何切它"},
    "materialize_view": {"name": "视图名（省略=默认视图）",
                         "limit": "本次最多入库几篇（1-40，默认 12；arXiv 限速）",
                         "reason": "一句话中文说明为何入库"},
    "upstream_clusters": {"min_count": "至少几篇库内论文同引（默认 2）", "limit": "最多返回簇数"},
    "related_papers": {"arxiv_id": "基准论文（需已 sync_citations）", "limit": "返回相似篇数"},
    "coverage_report": {"sample_missing": "缺卡清单长度（1-50，默认 15）"},
    "stats_timeseries": {"days": "回溯窗口天数（1-365，默认 30）"},
    "watch_authors": {"days": "近 N 天新提交（默认 7）", "max_authors": "最多监控几位作者"},
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

    # ============================================================== M4 调研基建：引文网络/覆盖率/趋势/作者
    @reg.tool(name="sync_citations", kind="write", reversible=True,
              description="把在库论文的引用边（它引用了谁）落库成本地图谱；重跑幂等替换。"
                          "**支持逗号分隔多篇一次织**（curated 画图前把清单里的边一次补齐，"
                          "别为三十篇调三十次）。落完 upstream_clusters/related_papers 本地免费查。")
    def sync_citations(arxiv_id: str, limit: int = 40,
                       actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        ids = [x.strip() for x in (arxiv_id or "").replace("，", ",").split(",") if x.strip()]
        if not ids:
            return err("bad_params", "arxiv_id 为空",
                       hint="给一个或多个 arXiv 编号（逗号分隔），如 '2508.06639,1902.09551'")

        def _one(aid: str) -> dict:
            if repo.get_paper(aid) is None:
                return err("not_found", f"库里没有 {aid}",
                           hint="先 fetch_paper_by_id 拉进入库")
            client = _scholar()
            try:
                items = client.references(arxiv_ext_id(aid), limit=min(int(limit), 100))
            finally:
                client.close()
            rows = []
            for it in items:
                p = it.get("citedPaper") or {}
                ext = p.get("externalIds") or {}
                dst = (ext.get("ArXiv") or p.get("arxiv_id") or "").strip()
                if not dst:
                    continue                      # 只留有 arXiv 号的边（可溯可互引）
                rows.append({"arxiv_id": dst, "title": p.get("title") or "",
                             "citation_count": p.get("citationCount") or 0,
                             "year": p.get("year") or 0,      # 年代编排（timeline）要用
                             "influential": bool(it.get("isInfluential"))})
            out = repo.replace_citation_edges(aid, rows, actor=actor,
                                              reason=reason or f"落库引文边 {aid}")
            return ok(arxiv_id=aid, edges=out["added"], replaced=out["replaced"])

        if len(ids) == 1:                          # 单篇：回执形状与从前一致
            r = _one(ids[0])
            if r.get("ok"):
                r["hint"] = "本地图谱已更新；upstream_clusters 聚合簇、related_papers 找同伙"
            return r
        done: list[dict] = []
        failed: list[dict] = []
        for aid in ids:
            r = _one(aid)
            if r.get("ok"):
                done.append({"arxiv_id": aid, "edges": r["edges"]})
            else:
                failed.append({"arxiv_id": aid,
                               "error": (r.get("error") or {}).get("kind", "error")})
        return ok(count=len(done), failed=failed, items=done,
                  edges=sum(d["edges"] for d in done),
                  hint="清单里的边一次补齐；failed 的那些先 fetch_paper_by_id 入库再补织")

    @reg.tool(name="sync_cited_by", kind="write",
              description="反查'谁引用了这篇'入图（S2 citations → 反向边）：下游扩散在 /network 独立成层；"
                          "引用者可未入库（节点点击自动入库）。**支持逗号分隔多篇一次织**。"
                          "增量幂等加边，不可 undo（重建用 sync_citations）。")
    def sync_cited_by(arxiv_id: str, limit: int = 40,
                      actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        ids = [x.strip() for x in (arxiv_id or "").replace("，", ",").split(",") if x.strip()]
        if not ids:
            return err("bad_params", "arxiv_id 为空",
                       hint="给一个或多个 arXiv 编号（逗号分隔）")

        def _one(aid: str) -> dict:
            if repo.get_paper(aid) is None:
                return err("not_found", f"库里没有 {aid}",
                           hint="先 fetch_paper_by_id 拉进入库")
            client = _scholar()
            try:
                items = client.citations(arxiv_ext_id(aid), limit=min(int(limit), 100))
            finally:
                client.close()
            rows = []
            for it in items:
                p = it.get("citingPaper") or {}
                ext = p.get("externalIds") or {}
                src = (ext.get("ArXiv") or "").strip()
                if src and src != aid:
                    rows.append({"src": src, "dst": aid, "title": p.get("title") or src,
                                 "citations": p.get("citationCount") or 0,
                                 "year": p.get("year") or 0,
                                 "influential": bool(it.get("isInfluential"))})
            added = repo.add_citation_edges(rows, actor=actor,
                                            reason=reason or f"反查 {aid} 的下游扩散",
                                            target=aid)
            return ok(arxiv_id=aid, added=added)

        if len(ids) == 1:                          # 单篇：回执形状与从前一致
            r = _one(ids[0])
            if r.get("ok"):
                r["hint"] = f"/network?root={ids[0]} 刷新即可见下游层（引用者可未入库）"
            return r
        done: list[dict] = []
        failed: list[dict] = []
        for aid in ids:
            r = _one(aid)
            if r.get("ok"):
                done.append({"arxiv_id": aid, "added": r["added"]})
            else:
                failed.append({"arxiv_id": aid,
                               "error": (r.get("error") or {}).get("kind", "error")})
        return ok(count=len(done), failed=failed, items=done,
                  added=sum(d["added"] for d in done),
                  hint="下游一次补齐；failed 的那些先入库再补织")

    @reg.tool(name="tag_paper", kind="write", reversible=True,
              description="给在库论文钉一枚图论标签（六色：平台源头/理论源头/综述枢纽/"
                          "实验谱系/下游扩散/动机），一论文一枚、重跑替换；/network 图例即按此着色。")
    def tag_paper(arxiv_id: str, tag: str, actor: str = ACTOR_DEFAULT,
                  reason: str = "") -> dict:
        if repo.get_paper(arxiv_id) is None:
            return err("not_found", f"库里没有 {arxiv_id}",
                       hint="先 fetch_paper_by_id 拉进入库")
        if tag not in repo.TAGS:
            return err("bad_params", f"未知标签 {tag!r}",
                       hint="六色枚举：" + "/".join(repo.TAGS), suggest=list(repo.TAGS))
        out = repo.set_tag(arxiv_id, tag, actor=actor, reason=reason)
        return ok(arxiv_id=arxiv_id, tag=tag, replaced=out["replaced"],
                  hint="/network 把颜色依据切到 tag 即看图例；标错了 undo_change 还原旧标")

    @reg.tool(name="tag_papers", kind="write", reversible=True,
              description="**批量**钉标签（一次事件、可整批撤销）：items 形如 "
                          "'1707.04344:平台源头,1605.04570:平台源头,1208.1220:动机'。"
                          "四五十篇一次钉完，别一篇一个调用。")
    def tag_papers(items: str, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        from ..app import graph_view as _gv
        tags = set(_gv.TAG_COLORS)
        rows, bad = [], []
        for chunk in (items or "").replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            aid, _, tag = chunk.partition(":")
            aid, tag = aid.strip(), tag.strip()
            if not aid or tag not in tags:
                bad.append(chunk)
                continue
            if repo.get_paper(aid) is None:
                bad.append(f"{aid}(不在库)")
                continue
            rows.append({"arxiv_id": aid, "tag": tag})
        if not rows:
            return err("bad_params", "没有可钉的条目",
                       hint="格式 'arxiv:标签'，标签枚举：" + "/".join(sorted(tags)),
                       suggest=sorted(tags))
        out = repo.set_tags(rows, actor=actor, reason=reason or f"批量钉标 {len(rows)} 篇")
        return ok(count=out["count"], applied=out["applied"], skipped=bad,
                  tags=repo.tag_counts(),
                  hint="颜色/图例/角标即刻生效；错了 undo_change(seq=0) 整批还原")

    @reg.tool(name="query_tags", kind="read",
              description="读回已钉的图论标签（篇目 → 标签 + 各标签计数）：审计与二次编排用。")
    def query_tags() -> dict:
        m = repo.tag_map()
        return ok(tags=m, counts=repo.tag_counts(), count=len(m))

    @reg.tool(name="set_graph_view", kind="write", reversible=True,
              description="**发布一张图视图**——/network 无参数打开就渲染它（AI 画什么，页面显示什么）。"
                          "重名覆盖，is_default=True 时把默认指针挪过来。"
                          "**两种模式**：`mode=curated`＝**你点名的清单**（`layers='id:层号,…'` 就是内容："
                          "点名的才上图，写下的顺序即层内次序——**这张图表达的是你的理解**）；"
                          "`mode=auto`＝机器按 `root`+`depth` 替你捞一圈的**草稿**，供你读一眼再决定。"
                          "`sides=both` ⇒ 上游（它引的）在上、下游（引用它的）在下；"
                          "`place=center` ⇒ 按 `rank` 从行中心向两侧展开（骨干贴中轴、连线最短）；"
                          "`sort_within=align` ⇒ 按连接重心对齐（线最短，可与 center 同开）；"
                          "timeline 布局＝年代列；`group_by=tag` 出泳道标题；`color_by=auto` 时按标签着色。")
    def set_graph_view(name: str = "默认视图", root: str = "", depth: int = 2,
                       sides: str = "both",
                       layout: str = "layer", color_by: str = "auto",
                       group_by: str = "none", group_order: str = "",
                       label_mode: str = "auto", label_style: str = "title",
                       label_max: int = 18, label_auto_max: int = 90, badge: bool = True,
                       arrow_size: int = 13, max_nodes: int = 90, max_edges: int = 400,
                       group_quota: int = 0, sort_within: str = "weight",
                       size_by: str = "degree", layer_gap: int = 130, node_gap: int = 90,
                       in_lib_only: bool = False,
                       pin: str = "", rank: str = "", place: str = "lane",
                       layers: str = "", mode: str = "auto",
                       title: str = "",
                       group_map: str = "", group_colors: str = "",
                       is_default: bool = True,
                       actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        """⚠ 参数面＝视图 spec 的全集：**少一个就会被边界层静默吞掉**（只有签名里有的才能落到 spec）。"""
        from ..app import graph_view as _gv
        gmap = {}
        for chunk in (group_map or "").replace(";", ",").split(","):
            aid, _, grp = chunk.partition(":")
            if aid.strip() and grp.strip():
                gmap[aid.strip()] = grp.strip()
        gcol = {}
        for chunk in (group_colors or "").replace(";", ",").split(","):
            grp, _, col = chunk.partition(":")
            if grp.strip() and col.strip():
                gcol[grp.strip()] = col.strip()
        spec = _gv.normalize_spec({
            "root": root, "depth": depth, "sides": sides, "layout": layout,
            "color_by": color_by,
            "group_by": group_by, "group_order": group_order,
            "label_mode": label_mode, "label_style": label_style, "label_max": label_max,
            "label_auto_max": label_auto_max,
            "badge": badge, "arrow_size": arrow_size, "max_nodes": max_nodes,
            "max_edges": max_edges, "group_quota": group_quota, "sort_within": sort_within,
            "size_by": size_by, "layer_gap": layer_gap, "node_gap": node_gap,
            "in_lib_only": in_lib_only,
            "pin": pin, "rank": rank, "place": place, "layers": layers, "mode": mode,
            "title": title or name,
            "group_map": gmap, "group_colors": gcol,
        })
        out = repo.save_graph_view(name, spec, is_default=bool(is_default),
                                   actor=actor, reason=reason or f"发布视图 {name}")
        payload = _gv.build(repo, container.retrieval, container.settings, spec)
        return ok(name=out["name"], replaced=out["replaced"], is_default=out["is_default"],
                  spec=spec, stats=payload["stats"],
                  legend=[f"{lg['tag']}×{lg['count']}" for lg in payload["legend"]],
                  url=f"/network?view={out['name']}" if not out["is_default"] else "/network",
                  hint="页面即刻生效（无需重启）；回执 stats 里 shown/layers 可自查；"
                       "细节看 /network.json；发错了 undo_change 撤这一版")

    @reg.tool(name="query_graph_views", kind="read",
              description="列出已发布的图视图；**给 name 就把那一张整幅拉出来**"
                          "（完整 spec：模式/层号清单/层内次序/锚点/配色/标题）——"
                          "改图前先拉出来看，别凭记忆重写。")
    def query_graph_views(name: str = "") -> dict:
        want = (name or "").strip()
        if want:                                   # 拉出单张 = "把我的作品取回来"
            row = repo.get_graph_view(want)
            if row is None:
                return err("not_found", f"没有这张视图：{want}",
                           hint="不带 name 先列一下已发布的视图名")
            spec = row["spec"] or {}
            return ok(name=row["name"], is_default=row["is_default"], spec=spec,
                      ts=row["ts"], actor=row["actor"], reason=row["reason"],
                      named=len(spec.get("layers") or {}),
                      hint="改这张：把 spec 里的字段照抄进 set_graph_view、同名覆盖即可")
        views = repo.list_graph_views()
        return ok(views=[{"name": v["name"], "is_default": v["is_default"],
                          "title": (v["spec"] or {}).get("title", ""),
                          "mode": (v["spec"] or {}).get("mode", "auto"),
                          "root": (v["spec"] or {}).get("root", ""),
                          "named": len((v["spec"] or {}).get("layers") or {}),
                          "layout": (v["spec"] or {}).get("layout", ""),
                          "color_by": (v["spec"] or {}).get("color_by", "")}
                         for v in views],
                  count=len(views),
                  hint="要看/要改哪张，就用 query_graph_views(name='视图名') 把它整幅拉出来")

    @reg.tool(name="delete_graph_view", kind="write", reversible=True,
              description="**删除一张已发布的图视图**（**人类专属**：不投影给 AI——图的删除归人；"
                          "AI 想删请让人在 /settings 的「视图管理」里点）。")
    def delete_graph_view(name: str, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        out = repo.delete_graph_view(name, actor=actor,
                                     reason=reason or f"删除视图 {name}")
        return ok(**out,
                  hint="删除已进事件总线（存了 spec 快照）；/activity 可 undo，"
                       "或让 AI 调 undo_change(seq=0)")

    @reg.tool(name="set_default_view", kind="write", reversible=True,
              description="把某张已发布视图设为默认（/network 无参数渲染它）。")
    def set_default_view(name: str, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        out = repo.set_default_graph_view(name, actor=actor,
                                          reason=reason or f"默认视图切到 {name}")
        return ok(name=out["name"], previous=out["previous"],
                  url="/network", hint="刷新 /network 即是这张；undo_change 可回上一张")

    @reg.tool(name="materialize_view", kind="write", reversible=False,
              description="**把视图里还没入库的点全部入库**——图上的每篇论文都应是库内论文"
                          "（能点进管理页、能补卡、信号能进画像）。幂等、可反复调用直到 remaining=0；"
                          "每次受 limit 限制（arXiv 有 3s 限速），回执报 fetched/remaining。")
    def materialize_view(name: str = "", limit: int = 12,
                         actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        from ..app import graph_view as _gv
        row = repo.get_graph_view(name) if (name or "").strip() else repo.default_graph_view()
        if row is None:
            return err("not_found", f"没有视图 {name or '(默认)'}",
                       hint="先 set_graph_view 发一张，或 query_graph_views 看有哪些")
        spec = row["spec"] or {}
        cap = max(1, min(int(limit), 40))
        fetched: list[str] = []
        failed: list[str] = []
        # ⚠ 必须**边抓边重渲染**：论文一旦入库，节点权重变化会让选点集合漂移
        # （旧版只渲染一次就报 remaining，实测报 0 后仍缺 7 篇——回执不实就是 bug）。
        for _ in range(8):
            payload = _gv.build(repo, container.retrieval, container.settings, spec)
            missing = [n["id"] for n in payload["nodes"] if not n.get("in_lib")]
            if not missing or len(fetched) + len(failed) >= cap:
                break
            batch = missing[:max(1, min(8, cap - len(fetched) - len(failed)))]
            for aid in batch:
                res = reg.invoke("fetch_paper_by_id", arxiv_id=aid, actor=actor,
                                 reason=reason or f"入库视图节点（{row['name']}）")
                (fetched if res.get("ok") else failed).append(aid)
        payload = _gv.build(repo, container.retrieval, container.settings, spec)
        still = [n["id"] for n in payload["nodes"] if not n.get("in_lib")]
        return ok(view=row["name"], fetched=len(fetched), fetched_ids=fetched,
                  failed=failed, remaining=len(still), remaining_ids=still[:10],
                  total_nodes=len(payload["nodes"]),
                  hint=("继续调本工具直到 remaining=0" if still
                        else "视图节点已全部入库：每篇都能点进管理页/补卡"))

    @reg.tool(name="upstream_clusters", kind="read",
              description="关键上游簇：库内多篇反复引同一文献⇒该领域的思想源头；按入组数降序。")
    def upstream_clusters(min_count: int = 2, limit: int = 20) -> dict:
        clusters = repo.upstream_clusters(min_count=max(1, int(min_count)), limit=limit)
        if not clusters:
            return ok(clusters=[], hint="还没有多引上游：先对几篇关键论文 sync_citations")
        return ok(clusters=clusters, count=len(clusters))

    @reg.tool(name="related_papers", kind="read",
              description="共引相似度：与指定论文引用集交集最大的库内论文（谁和它在研究同一堆事）。")
    def related_papers(arxiv_id: str, limit: int = 10) -> dict:
        rel = repo.related_by_cocitation(arxiv_id, limit=limit)
        if not rel:
            return ok(related=[], hint=f"{arxiv_id} 本地无边或与库无交集：先 sync_citations 几篇")
        return ok(related=rel)

    @reg.tool(name="coverage_report", kind="read",
              description="调研资产覆盖率：多少篇有卡/读过/收藏，最新缺卡清单——就是 write_summary 的工单。")
    def coverage_report(sample_missing: int = 15) -> dict:
        rep = repo.coverage_report(sample_missing=max(1, min(int(sample_missing), 50)))
        return ok(**rep, hint="缺卡清单按新→旧；挑要紧的 write_summary 补，别一次刷满惊跑写权门")

    @reg.tool(name="stats_timeseries", kind="read",
              description="趋势聚合（近 N 天）：每日入库、信号漏斗计数、简报节奏、AI 成本按用途汇总。")
    def stats_timeseries(days: int = 30) -> dict:
        if not 1 <= int(days) <= 365:
            return err("bad_params", f"days={days} 越界（1-365）")
        return ok(**repo.stats_timeseries(days=int(days)))

    @reg.tool(name="watch_authors", kind="read",
              description="作者监控：主题关注作者 + 画像正权作者的近 N 天新提交（只读报告；"
                          "要入库逐篇 fetch_paper_by_id，不自动写库）。")
    def watch_authors(days: int = 7, max_authors: int = 12) -> dict:
        names: list[str] = []
        for t in settings.topics:
            for a in (getattr(t, "authors", []) or []):
                if a and a not in names:
                    names.append(a)
        for key, w, _h in repo.profile_view()["top"]["author"]:
            if w > 0 and key not in names:
                names.append(key)
        names = names[:max(1, int(max_authors))]
        if not names:
            return err("no_authors", "当前没有关注作者",
                       hint="update_topic 给主题加 authors，或靠 record_signal 攒出画像作者权重")
        client = ArxivClient(cache_dir=settings.cache_dir / "arxiv")
        try:
            found = client.fetch_authors(
                names, since=datetime.utcnow() - timedelta(days=max(1, int(days))))
        finally:
            client.close()
        return ok(days=days, watched=names, authors={
            a: [{"arxiv_id": p.arxiv_id, "title": p.title,
                 "published": p.published_at.date().isoformat() if p.published_at else ""}
                for p in ps] for a, ps in found.items()},
            hint="只读报告；要哪篇就 fetch_paper_by_id，顺带 record_signal(download) 喂画像")

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

    # ⭐ 声明与行为对齐（2026-09-26）：它**不改业务状态**（预览不落库、不建 feed 期——建期的是
    # `publish_feed`），但会追加一条 `telemetry.*` 痕 ⇒ 这正是"读 + 遥测"那一档。
    # ⚠ 框架没有这一档（ADR `读写遥测三档不进框架`）⇒ 由项目侧判据对账（`tests/test_telemetry_kinds.py`）。
    @reg.tool(name="feed_generate", kind="read_telemetry",
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
        # ⚠ op 名带 **`telemetry.` 前缀**（2026-09-26）：按《接入指南》第 4 步的**前缀分离**
        # 口径（`telemetry.*` 与 `signal:*` 互不计入）。**逻辑一字未改**，只改了名字
        # ⇒ `/activity` 与 `get_activity(op=…)` 的过滤串随之变化（已申报）。
        repo.record_op("telemetry.feed_generate", actor=actor,
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

    # ============================ 精读（arXiv HTML 正文 + 带位置的批注）
    # 这是 docs/SPEC.md N2（全文解析，供精读）的落地，但**改用 HTML 而非 PDF**：HTML 有稳定 id
    # （S3.p2 / S3.F1 / S3.E1）⇒ 能渲染、能锚定、能分节喂 AI。分工与画图同一套：
    # **判断归 AI**（读哪节、标哪句、批注写什么），**几何归代码**（quote→字符区间、资源离线化、
    # 页面渲染、无头截图）。明说的取舍：**arXiv 没提供 HTML 的论文不进这套体系**（如实报
    # no_html，不回落 PDF——PDF 没有锚点，标不住）。
    html_root = Path(settings.data_dir) / "paper_html"
    shot_root = Path(settings.data_dir) / "shots"

    def _html_dir(aid: str, version: int) -> Path:
        return html_root / aid / f"v{int(version or 0)}"

    def _version_of(url: str, html_text: str = "", aid: str = "") -> int:
        """认出正文的 arXiv 版本号。

        ⚠ 实测：arXiv 常把 `/html/<id>` 直接 302 到**不带版本号**的地址，而正文里的资源路径
        写着 `/html/<id>v1/…` ⇒ **只认 URL 会得到 0**，而版本号是"换版本后批注是否失锚"的依据。
        所以 URL 认不出时**从正文里认**（这才是真的那个版本）。
        """
        tail = (url or "").rstrip("/").split("/")[-1]
        if "v" in tail:
            num = tail.rsplit("v", 1)[1]
            if num.isdigit():
                return int(num)
        if aid and html_text:
            for marker in (f"/{aid}v", f"abs/{aid}v"):
                i = html_text.find(marker)
                while i >= 0:
                    j, num = i + len(marker), ""
                    while j < len(html_text) and html_text[j].isdigit():
                        num += html_text[j]
                        j += 1
                    if num:
                        return int(num)
                    i = html_text.find(marker, i + 1)
        return 0

    def _load_blocks(aid: str) -> tuple[dict | None, list[Block]]:
        meta = repo.get_paper_html(aid)
        if not meta or meta.get("status") != "ok":
            return meta, []
        index = _html_dir(aid, meta["version"]) / "index.html"
        if not index.exists():
            return meta, []
        return meta, extract_blocks(LH.fromstring(index.read_text(encoding="utf-8")))

    def _need_html(aid: str):
        """统一前置：没有正文就给出**可教学**的下一步（别让调用方猜）。"""
        meta = repo.get_paper_html(aid)
        if meta is None:
            return None, err("no_html_archived", f"{aid} 还没归档 HTML 正文",
                             hint="先调 fetch_paper_html 抓一份（arXiv 没 HTML 的抓不到）")
        if meta.get("status") != "ok":
            return meta, err(
                "no_html", f"{aid} 没有可用的 HTML 正文：{meta.get('detail') or 'arXiv 未提供'}",
                hint="这篇不进精读体系——只有 arXiv 提供 HTML 的论文才有精读页")
        return meta, None

    def _web_base() -> str:
        """读项目根 ``.web-port`` 找正在跑的 Web（截图必须拍真实页面，不另起服务）。"""
        port_file = Path(settings.data_dir).parent / ".web-port"
        port = port_file.read_text(encoding="utf-8").strip() if port_file.exists() else ""
        return f"http://127.0.0.1:{port}" if port.isdigit() else ""

    def _section_slice(blocks: list[Block], name: str) -> list[Block]:
        """按节名取一段（节名**包含匹配**）；摘要/文献表也能这样显式取到。"""
        kinds = ("section", "bibliography", "abstract")
        key = (name or "").strip().lower()
        start = next((i for i, b in enumerate(blocks)
                      if b.kind in kinds and key in (b.text or "").lower()), None)
        if start is None:
            return []
        end = next((j for j in range(start + 1, len(blocks)) if blocks[j].kind in kinds),
                   len(blocks))
        return blocks[start:end]

    @reg.tool(name="fetch_paper_html", kind="write",
              description="下载并归档一篇论文的 **HTML 正文**（精读体系的地基）：剥脚本与事件属性、"
                          "**全量离线**抓下样式与图片并改写为站内路径。arXiv 未提供 HTML 的论文"
                          "如实报 no_html（不进精读体系，不回落 PDF）。重跑幂等、不可 undo。")
    def fetch_paper_html(arxiv_id: str, force: bool = False,
                         actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        aid = (arxiv_id or "").strip()
        if not aid:
            return err("bad_params", "arxiv_id 为空", hint="给一个 arXiv 编号，如 2508.06639")
        if repo.get_paper(aid) is None:
            return err("not_found", f"库里没有 {aid}", hint="先 fetch_paper_by_id 拉进入库")
        old = repo.get_paper_html(aid)
        if old and old.get("status") == "ok" and not force:
            return ok(cached=True, version=old["version"], source=old["source"],
                      sha256=old["sha256"], bytes=old["bytes"], assets=old["assets"],
                      asset_bytes=old["asset_bytes"], blocks=old["blocks"], chars=old["chars"],
                      url=f"/read/{aid}", hint="已归档；要重抓传 force=True")
        client = PaperHtmlClient()
        try:
            res = fetch_html(client, aid)
            if not res.ok:
                repo.save_paper_html(aid, status="no_html", detail=res.detail,
                                     actor=actor, reason=reason or f"归档 HTML 正文 {aid}")
                return err("no_html", f"{aid} 没有 HTML 正文：{res.detail}",
                           hint="这篇不进精读体系——arXiv 只为部分论文产出 HTML")
            version = _version_of(res.url, res.html, aid)
            dest = _html_dir(aid, version)
            dest.mkdir(parents=True, exist_ok=True)
            clean, stats = localize_and_clean(res.html, base_url=res.url, client=client,
                                             dest=dest, app_prefix=f"/paper/{aid}")
            (dest / "index.html").write_text(clean, encoding="utf-8")
        finally:
            client.close()
        blocks = extract_blocks(LH.fromstring(clean))
        indexed = repo.index_paper_text(aid, version, blocks)   # 块级全文索引（融合检索）
        meta = repo.save_paper_html(
            aid, version=version, source=res.source, source_url=res.url, status="ok",
            detail="", sha256=sha256_text(clean), bytes=len(clean.encode("utf-8")),
            assets=stats.get("assets", 0), asset_bytes=stats.get("bytes", 0),
            blocks=len(blocks), chars=sum(len(b.text) for b in blocks),
            actor=actor, reason=reason or f"归档 HTML 正文 {aid}")
        return ok(**meta, source_url=res.url, url=f"/read/{aid}", cached=False,
                  text_blocks_indexed=indexed,
                  failed_assets=stats.get("failed", [])[:5],
                  hint="去 /read/{aid} 读；要 AI 看一眼版面就 capture_paper_shot")

    @reg.tool(name="search_library_text", kind="read",
              description="在**已归档正文**里跨篇检索（**块级**）：命中回 arxiv_id + 块 id + 高亮片段，"
                          "可一步跳到原文那一段。与 search_papers（标题/摘要/卡片）互补——"
                          "那个答「哪篇相关」，这个答「原文在哪说」。")
    def search_library_text(q: str, limit: int = 20) -> dict:
        hits = repo.search_text(q, limit=max(1, min(50, int(limit or 20))))
        return ok(q=q, count=len(hits), hits=hits, indexed_blocks=repo.text_index_size(),
                  hint="命中给的是**块**：用 read_paper_text(block=…) 读全段；"
                       "要人去看就把 /read/{arxiv_id} 给他")

    @reg.tool(name="read_paper_outline", kind="read",
              description="一篇论文的**章节树 + 锚点地图**：每节的块数与类型分布、块 id 样例。"
                          "进正文前先看它——它告诉你「有什么、每块叫什么 id」，"
                          "后面 read_paper_text / search_paper_text / annotate_paper 都吃这些 id。")
    def read_paper_outline(arxiv_id: str) -> dict:
        meta, e = _need_html(arxiv_id)
        if e:
            return e
        _, blocks = _load_blocks(arxiv_id)
        return ok(arxiv_id=arxiv_id, version=meta["version"], chars=meta["chars"],
                  blocks=len(blocks), sections=blocks_outline(blocks),
                  first_ids=[b.block_id for b in blocks[:8]], url=f"/read/{arxiv_id}",
                  hint="要正文就 read_paper_text(section=…|block=…)；标哪句用 annotate_paper(quote=…)")

    @reg.tool(name="read_paper_text", kind="read",
              description="读论文正文（分块、带块 id）：可按 section / block 取，也可从头顺读。"
                          "回程有体积闸，截断时回 next_offset 告诉你从哪继续（不静默丢内容）。")
    def read_paper_text(arxiv_id: str, section: str = "", block: str = "",
                        offset: int = 0, limit: int = 6000) -> dict:
        meta, e = _need_html(arxiv_id)
        if e:
            return e
        _, blocks = _load_blocks(arxiv_id)
        if block:
            picked = [b for b in blocks if b.block_id == block]
            if not picked:
                return err("block_not_found", f"没有块 {block}",
                           hint="先用 read_paper_outline 看有哪些块 id")
        elif section:
            picked = _section_slice(blocks, section)
            if not picked:
                return err("section_not_found", f"没有匹配「{section}」的章节",
                           hint="节名按包含匹配；用 read_paper_outline 看现有节名")
        else:
            # 顺读时**跳过参考文献整表**：一篇 78 条文献能把体积闸一下占满，
            # 把真正的正文挤出回程。要读文献表就显式 `section="References"`。
            picked = [b for b in blocks if b.kind != "bibliography"]
        start = max(0, int(offset or 0))
        cap = max(500, min(20000, int(limit or 6000)))
        window = picked[start:]
        out: list[dict] = []
        used = 0
        for b in window:
            text = b.text.strip()
            if out and used + len(text) > cap:
                break
            out.append({"id": b.block_id, "kind": b.kind, "section": b.section, "text": text})
            used += len(text)
        more = start + len(out)
        truncated = more < len(picked)
        return ok(arxiv_id=arxiv_id, blocks=out, truncated=truncated,
                  total_blocks=len(picked), next_offset=more if truncated else 0,
                  where=(f"还有 {len(picked) - more} 块没回；再调 offset={more} 续读"
                         if truncated else ""))

    @reg.tool(name="search_paper_text", kind="read",
              description="在**一篇论文的正文里**检索：命中带块 id 与前后文——拿它定位"
                          "「这句话在哪一块」，再用那个 block 去 annotate_paper 就精确了。")
    def search_paper_text(arxiv_id: str, q: str, limit: int = 8) -> dict:
        meta, e = _need_html(arxiv_id)
        if e:
            return e
        _, blocks = _load_blocks(arxiv_id)
        needle = (q or "").strip()
        if not needle:
            return err("bad_params", "q 为空", hint="给一个原文里出现过的词或短语")
        keep = max(1, min(30, int(limit or 8)))
        hits: list[dict] = []
        for b in blocks:
            i = b.text.find(needle)
            if i >= 0:
                hits.append({"block": b.block_id, "kind": b.kind, "section": b.section,
                             "start": i, "snippet": snippet(b.text, i, i + len(needle))})
                if len(hits) >= keep:
                    break
        return ok(arxiv_id=arxiv_id, q=needle, count=len(hits), hits=hits,
                  hint="annotate_paper(quote=…) 直接给原句也行；多处命中时用 block 指定")

    @reg.tool(name="annotate_paper", kind="write", reversible=True,
              description="在论文原文上**加带位置的批注**：给一句原文（quote）或一个块 id，"
                          "由后端解析成精确字符区间再落库（段落/句子/公式/图表都能标）。"
                          "多处命中时回候选让你用 block 指定；找不到就报 not_found（别硬标）。"
                          "回执里的 snippet 就是「标在哪」的证据。")
    def annotate_paper(arxiv_id: str, quote: str = "", block: str = "", body: str = "",
                       color: str = "", kind: str = "highlight",
                       actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        meta, e = _need_html(arxiv_id)
        if e:
            return e
        _, blocks = _load_blocks(arxiv_id)
        if quote:
            hit = locate_quote(blocks, quote, block_id=block)
            if hit.reason == "block_not_found":
                return err("block_not_found", f"没有块 {block}",
                           hint="用 read_paper_outline 看块 id")
            if hit.reason == "ambiguous":
                return err("ambiguous", f"这句话在全文里出现 {hit.candidates} 次",
                           hint="用 block 指定是哪一块（suggest 里是候选）",
                           suggest=[a.as_dict() for a in hit.anchors])
            if not hit.anchors:
                return err("not_found", "没在正文里找到这句原文",
                           hint="换一句更独特的原文；或先 search_paper_text 拿 block")
            anchor = hit.anchors[0]
        elif block:
            blk = next((b for b in blocks if b.block_id == block), None)
            if blk is None:
                return err("block_not_found", f"没有块 {block}",
                           hint="用 read_paper_outline 看块 id")
            anchor = Anchor(block=block, start=0, end=len(blk.text), quote="", kind=blk.kind)
        else:
            return err("bad_params", "quote 与 block 至少给一个",
                       hint="标一句就 quote='原文那句话'；标整块/整图就 block='S3.F1'")
        snap = repo.add_mark(arxiv_id, anchor=anchor.as_dict(),
                             quote=anchor.quote or quote, body=body, kind=kind,
                             color=color, html_sha256=meta["sha256"],
                             actor=actor, reason=reason)
        blk = next((b for b in blocks if b.block_id == anchor.block), None)
        return ok(mark=snap, block=anchor.block, kind=anchor.kind,
                  snippet=snippet(blk.text, anchor.start, anchor.end) if blk else "",
                  url=f"/read/{arxiv_id}#m{snap['id']}",
                  hint="位置对不对看 snippet；要核对观感就 capture_paper_shot")

    @reg.tool(name="update_mark", kind="write", reversible=True,
              description="改一条批注（正文/状态/颜色）。改完回执仍是那条批注的完整快照。")
    def update_mark(mark_id: int, body: str = "", status: str = "", color: str = "",
                    actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        out = repo.update_mark(int(mark_id), body=body if body else None,
                               status=status if status else None,
                               color=color if color else None,
                               actor=actor, reason=reason or f"改批注 {mark_id}")
        if out is None:
            return err("not_found", f"没有批注 {mark_id}", hint="verify_marks 看现有批注 id")
        return ok(mark=out, hint="改完可 undo_change(seq=0) 撤这一版")

    @reg.tool(name="resolve_mark", kind="write", reversible=True,
              description="把一条批注标为**已解决**（问题处理完了，但痕迹留着）。")
    def resolve_mark(mark_id: int, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        out = repo.update_mark(int(mark_id), status="resolved", actor=actor,
                               reason=reason or f"标记已解决 {mark_id}")
        if out is None:
            return err("not_found", f"没有批注 {mark_id}", hint="verify_marks 看现有批注 id")
        return ok(mark=out, hint="已解决；要重新打开用 update_mark(status='active')")

    @reg.tool(name="delete_mark", kind="write", reversible=True,
              description="**删除一条批注**（**人类专属**：不投影给 AI——精读痕迹的处置权归人；"
                          "AI 想撤掉自己刚写的那条，用 undo_change）。")
    def delete_mark(mark_id: int, actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        out = repo.delete_mark(int(mark_id), actor=actor, reason=reason or f"删除批注 {mark_id}")
        if out is None:
            return err("not_found", f"没有批注 {mark_id}", hint="verify_marks 看现有批注 id")
        return ok(**out, hint="删除已进事件总线；undo_change(seq=0) 可恢复整条")

    @reg.tool(name="verify_marks", kind="read",
              description="**批注回执**：逐条报「重锚结果 + 命中的原句 + 前后文」。标完先看它——"
                          "位置错了一眼看得出来，不必等人截图。")
    def verify_marks(arxiv_id: str, since_id: int = 0) -> dict:
        meta, e = _need_html(arxiv_id)
        if e:
            return e
        _, blocks = _load_blocks(arxiv_id)
        marks = repo.marks_for(arxiv_id, since_id=int(since_id or 0))
        rep: list[dict] = []
        for m in marks:
            a = m.get("anchor") or {}
            blk = next((b for b in blocks if b.block_id == a.get("block")), None)
            start, end = int(a.get("start") or 0), int(a.get("end") or 0)
            rep.append({"id": m["id"], "kind": m["kind"], "status": m["status"],
                        "block": a.get("block", ""), "resolved": blk is not None,
                        "quote": (m["quote"] or "")[:120], "body": (m["body"] or "")[:120],
                        "snippet": snippet(blk.text, start, end) if blk else "",
                        "url": f"/read/{arxiv_id}#m{m['id']}"})
        bad = [r["id"] for r in rep if not r["resolved"]]
        return ok(arxiv_id=arxiv_id, count=len(rep), marks=rep, unresolved=bad,
                  version=meta["version"],
                  hint=("有批注的块对不上（正文变过？）⇒ 用 update_mark 重锚；"
                        "要看观感用 capture_paper_shot" if bad
                        else "全部锚定正常；要确认排版观感再 capture_paper_shot"))

    @reg.tool(name="capture_paper_shot", kind="write",
              description="**服务端无头截图**：把真实阅读页拍成 PNG 存到本地并回你文件路径——"
                          "你用它旁边的 read_image 打开，就能看见自己标的批注长什么样、"
                          "挡没挡住正文。「位置对但观感不对」只有这一条检查手段。")
    def capture_paper_shot(arxiv_id: str, mark_id: int = 0, width: int = 1440,
                           actor: str = ACTOR_DEFAULT, reason: str = "") -> dict:
        meta, e = _need_html(arxiv_id)
        if e:
            return e
        base = _web_base()
        if not base:
            return err("web_not_running", "读不到 .web-port（Web 没在跑）",
                       hint="先起 Web（paperpilot serve / ai）；截图拍的是真实页面，不另起服务")
        url = f"{base}/read/{arxiv_id}" + (f"?focus={int(mark_id)}" if mark_id else "")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-m{int(mark_id)}.png" if mark_id else f"{stamp}-full.png"
        out = shot_root / arxiv_id / name
        try:
            # **自证**：先问页面"标记实际渲染成什么样"（计算样式 + 包围盒），再拍照。
            # 这是"效果对不对"的机读证据——不靠人（或 AI）盯着截图猜。
            raw = shot_dump_attr(url, "data-pp-marks")
            rendered = json.loads(raw) if raw.strip().startswith("[") else []
            info = shot_capture(url, out, width=max(600, min(2400, int(width or 1440))),
                                full_page=not bool(mark_id))
        except ShotError as exc:
            return err("shot_unavailable", str(exc),
                       hint="装 Edge 或 Chrome 任一即可（Chromium 内核）——本机零新依赖方案")
        bad = [d for d in rendered if d.get("error") or not d.get("h")]
        return ok(arxiv_id=arxiv_id, path=info["path"], width=info["width"],
                  height=info["height"], bytes=info["bytes"], page=url,
                  too_tall=info["height"] >= 8000,
                  marks_rendered=len(rendered), marks_broken=[d.get("id") for d in bad],
                  render_diag=rendered[:8],
                  hint=("这篇太长，整页图到了 8000px 上限——**多模态读图读不了这么高的图**；"
                        "改拍局部：capture_paper_shot(mark_id=…) 或调小 width" if info["height"] >= 8000
                        else "用 read_image 打开这个 path 看观感；render_diag 是每个标记的"
                             "实际底色/边框/尺寸（机读证据，比目测靠得住）"))

    @reg.tool(name="read_paper_shots", kind="read",
              description="列出这篇论文已拍过的截图（最近优先），回本地路径——交给 read_image 看。")
    def read_paper_shots(arxiv_id: str, limit: int = 5) -> dict:
        d = shot_root / arxiv_id
        files = (sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
                 if d.exists() else [])
        keep = max(1, min(20, int(limit or 5)))
        return ok(arxiv_id=arxiv_id, count=len(files),
                  shots=[{"path": str(p), "bytes": p.stat().st_size,
                          "ts": datetime.fromtimestamp(p.stat().st_mtime).isoformat(
                              timespec="seconds")} for p in files[:keep]],
                  hint="用 read_image 打开 path 看真实渲染效果")

    reg.attach_param_descriptions(PARAM_DESCRIPTIONS)   # N8：单一事实源装入 ToolSpec.params
    return reg
