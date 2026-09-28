"""SQLite 仓储：domain/ports/repo.py 契约的 SQLAlchemy 实现。

归因与记录仪（docs/GAPS.md §2/§3）：**所有写入手动接受 `actor`/`reason` 关键字**，
并在同一事务内 append 一条 `events`（append-only，只增不改由 DB 触发器钉死）。
actor 约定：MCP 工具 = "ai"，Web/CLI = "human"，调度 = "scheduler"，内部 = "system"/"pipeline"。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import TopicCfg
from ..domain.models import PaperSummary, RelevanceScore
from ..infra.ai.errors import AIError
from .arxiv import NormalizedPaper
from .fts import PaperIndex
from .orm import (
    AICall,
    Base,  # noqa: F401  （re-export 便于外部 import）
    Briefing,
    Event,
    Note,
    Paper,
    PaperScore,
    PaperSummaryRow,
    ReadingState,
    Run,
    Topic,
    utcnow,
)

logger = logging.getLogger("paperpilot.repo")

_PAPER_EAGER = (
    selectinload(Paper.scores),
    selectinload(Paper.summaries),
    selectinload(Paper.reading),
    selectinload(Paper.notes),
)

# 阅读态三元的字段名（undo 回写用）
_READING_FIELDS = ("read", "star", "marked_skip")


class PaperRepository:
    def __init__(self, session_factory, index: PaperIndex | None = None) -> None:
        self.sf = session_factory
        self.index = index

    # ---------------------------------------------------------------- 事件（L2 记录仪）
    def _event(
        self,
        s: Session,
        *,
        op: str,
        actor: str,
        reason: str = "",
        target: str = "",
        before: dict | None = None,
        after: dict | None = None,
        reversible: int = 1,
    ) -> Event:
        """在**当前事务内** append 一条事件（调用方负责 commit）。"""
        event = Event(
            actor=actor or "system",
            reason=reason or "",
            op=op,
            target=target,
            before=before,
            after=after,
            reversible=reversible,
        )
        s.add(event)
        return event

    def events_since(
        self,
        *,
        since_seq: int = 0,
        actor: str = "",
        op: str = "",
        limit: int = 50,
    ) -> dict:
        """diff-since-seq 读事件（只读；GAPS.md §3）。"""
        with self.sf() as s:
            stmt = select(Event)
            if since_seq:
                stmt = stmt.where(Event.seq > since_seq)
            if actor:
                stmt = stmt.where(Event.actor == actor)
            if op:
                stmt = stmt.where(Event.op == op)
            stmt = stmt.order_by(Event.seq.desc()).limit(max(1, min(int(limit), 200)))
            events = list(s.scalars(stmt))
            last_seq = s.scalar(select(func.max(Event.seq))) or 0
            return {
                "last_seq": int(last_seq),
                "count": len(events),
                "events": [
                    {
                        "seq": e.seq,
                        "ts": e.ts.isoformat() if e.ts else None,
                        "actor": e.actor,
                        "reason": e.reason,
                        "op": e.op,
                        "target": e.target,
                        "reversible": e.reversible,
                        "before": e.before,
                        "after": e.after,
                    }
                    for e in events
                ],
            }

    def undo(self, seq: int = 0, *, actor: str, reason: str = "") -> dict:
        """按 seq 撤销可逆操作（seq=0 = 最近一条可逆事件）。不可逆操作明确拒绝。"""
        with self.sf() as s:
            if seq:
                event = s.get(Event, int(seq))
            else:
                event = s.scalar(
                    select(Event).where(Event.reversible == 1).order_by(Event.seq.desc())
                )
            if event is None:
                raise AIError(
                    "没有可撤销的操作" if not seq else f"事件 #{seq} 不存在",
                    kind="nothing_to_undo",
                    hint="undo(seq=0) 撤销最近一条可逆事件；先用 get_activity 看事件列表",
                )
            if event.reversible != 1:
                raise AIError(
                    f"操作 #{event.seq}（{event.op}）不可逆",
                    kind="irreversible",
                    hint="不可逆操作（抓取入库/简报定稿）按设计拒绝撤销；"
                    "简报可重跑生成新版本，入库论文无法单独撤",
                )
            before_now = self._restore(s, event)
            self._event(
                s,
                op="undo",
                actor=actor,
                reason=reason or f"undo #{event.seq}（{event.op}）",
                target=f"event:{event.seq}",
                before=before_now,
                after=dict(event.before or {}),
                reversible=0,
            )
            s.commit()
            return {
                "ok": True,
                "undone_seq": event.seq,
                "op": event.op,
                "restored": dict(event.before or {}),
            }

    def _restore(self, s: Session, event: Event) -> dict:
        """把 event.before 的快照写回状态；返回撤销前的状态（供 undo 事件记账）。"""
        before = dict(event.before or {})
        op = event.op

        if op in ("set_read", "star_paper", "skip_paper"):
            paper = s.scalar(select(Paper).where(Paper.arxiv_id == before.get("arxiv_id")))
            if paper is None:
                raise AIError(f"论文 {before.get('arxiv_id')} 已不在库中，无法撤销",
                              kind="restore_failed", hint="论文可能被清理；重跑 fetch_papers 入库")
            row = self._ensure_reading(s, paper)
            snapshot = {f: bool(getattr(row, f)) for f in _READING_FIELDS}
            for f in _READING_FIELDS:
                if f in before:
                    setattr(row, f, bool(before[f]))
            return {"arxiv_id": paper.arxiv_id, **snapshot}

        if op == "mark_status":
            paper = s.scalar(select(Paper).where(Paper.arxiv_id == before.get("arxiv_id")))
            if paper is None:
                raise AIError(f"论文 {before.get('arxiv_id')} 已不在库中，无法撤销",
                              kind="restore_failed", hint="论文可能被清理")
            snapshot = {"arxiv_id": paper.arxiv_id, "status": paper.status}
            if before.get("status"):
                paper.status = before["status"]
            return snapshot

        if op == "add_note":
            note_id = (event.after or {}).get("note_id")
            note = s.get(Note, note_id) if note_id else None
            if note is not None:
                s.delete(note)
            return {"deleted_note_id": note_id}

        if op == "delete_note":
            note = Note(
                id=before.get("note_id"),
                paper_id=before.get("paper_id"),
                content=before.get("content", ""),
            )
            s.add(note)
            return {"restored_note_id": before.get("note_id")}

        if op == "sync_topics":
            snapshot = {"topics": self._topic_snapshot(s)}
            self._apply_topic_snapshot(s, before.get("topics") or [])
            return snapshot

        if op == "reset_statuses":
            items = before.get("items") or []
            by_id = {p.arxiv_id: p for p in s.scalars(select(Paper))}
            snapshot = {"items": []}
            for item in items:
                paper = by_id.get(item.get("arxiv_id"))
                if paper is None:
                    continue
                snapshot["items"].append({"arxiv_id": paper.arxiv_id, "status": paper.status})
                if item.get("status"):
                    paper.status = item["status"]
            return snapshot

        if op == "delete_briefing":
            # 重加被删的简报行（before 存了 markdown+stats，可完整还原最新一份）
            s.add(Briefing(
                date=before.get("date"), run_id=before.get("run_id"),
                title=before.get("title", ""), markdown=before.get("markdown", ""),
                stats=dict(before.get("stats") or {}),
                ai_enabled=bool(before.get("ai_enabled")),
                status=before.get("status", "draft"),
            ))
            return {"restored_briefing_date": before.get("date")}

        if op == "profile_reset":
            # 还原被重置的画像行（快照含 w/hits，忠实重建）
            from .orm import ProfileWeight
            snap = (event.before or {}).get("rows") or []
            for item in snap:
                s.add(ProfileWeight(kind=item["kind"], key=item["key"],
                                    w=float(item.get("w", 0.0)),
                                    hits=int(item.get("hits", 0))))
            return {"restored_profile_rows": len(snap)}

        if op == "save_feed":
            from .orm import FeedIssue
            iid = int((event.after or {}).get("id") or 0)
            row = s.get(FeedIssue, iid) if iid else None
            if row is not None:
                s.delete(row)
            return {"removed_feed_issue": iid}

        if op == "tag_paper":
            from .orm import PaperTag
            tid = event.target or ""
            row = s.get(PaperTag, tid) if tid else None
            old = (event.before or {}).get("tag") or ""
            if row is not None:
                if old:
                    row.tag = old
                else:
                    s.delete(row)
            return {"restored_tag": old or "(removed)"}

        if op == "set_tags":
            from .orm import PaperTag
            rows = ((event.before or {}).get("tags") or [])
            for item in rows:
                aid = item.get("arxiv_id") or ""
                old = item.get("tag") or ""
                if not aid:
                    continue
                row = s.get(PaperTag, aid)
                if old:
                    if row is None:
                        s.add(PaperTag(arxiv_id=aid, tag=old, actor=event.actor))
                    else:
                        row.tag = old
                elif row is not None:
                    s.delete(row)
            return {"restored_tags": len(rows)}

        if op == "set_graph_view":
            from .orm import GraphView
            name = event.target or ""
            before = event.before or {}
            row = s.get(GraphView, name) if name else None
            if before.get("existed"):
                if row is None:
                    row = GraphView(name=name)
                    s.add(row)
                row.spec = before.get("spec") or {}
                row.actor = before.get("actor") or ""
                row.reason = before.get("reason") or ""
                row.is_default = bool(before.get("is_default"))
                return {"restored_view": name}
            if row is not None:
                s.delete(row)
            return {"removed_view": name}

        if op == "set_default_view":
            from .orm import GraphView
            prev = (event.before or {}).get("default") or ""
            for v in s.scalars(select(GraphView)).all():
                v.is_default = bool(prev) and v.name == prev
            return {"restored_default": prev or "(none)"}

        if op == "sync_citations":
            # 回滚引文边：先把 src 现边清空，再按 before 快照重建（忠实回上一轮）
            from .orm import CitationEdge
            src = event.target or ""
            for e in s.scalars(select(CitationEdge).where(
                    CitationEdge.src_arxiv_id == src)).all():
                s.delete(e)
            s.flush()
            restored = 0
            for item in (event.before or {}).get("edges") or []:
                s.add(CitationEdge(src_arxiv_id=src, dst_arxiv_id=item["dst"],
                                   dst_title=item.get("title", ""),
                                   dst_citations=int(item.get("citations", 0)),
                                   influential=bool(item.get("infl")),
                                   direction=item.get("direction", "cites"),
                                   year=int(item.get("year") or 0)))
                restored += 1
            return {"restored_edges": restored}

        if op == "write_summary":
            # 撤销补卡：删掉那一轮的总结行与评分行（同 run_id 成对）
            from .orm import PaperScore, PaperSummaryRow
            rid = (event.after or {}).get("run_id") or ""
            removed = 0
            if rid:
                for model in (PaperSummaryRow, PaperScore):
                    for row in s.scalars(select(model).where(model.run_id == rid)).all():
                        s.delete(row)
                        removed += 1
            return {"removed_card_rows": removed}

        raise AIError(
            f"操作 {op} 不支持撤销",
            kind="unsupported_undo",
            hint="目前支持：阅读态/笔记/主题同步/状态重置/删简报/画像重置/钉标签/"
                 "视图发布的撤销",
        )

    # ---------------------------------------------------------------- topics
    def sync_topics(
        self, topics: Sequence[TopicCfg], *, actor: str = "system", reason: str = ""
    ) -> list[Topic]:
        """以配置为唯一事实源：更新/新增/删除 DB 镜像主题（配置删掉的主题，其历史打分的 topic_id 置空）。"""
        wanted: dict[str, TopicCfg] = {t.name: t for t in topics}
        with self.sf() as s:
            before_snapshot = self._topic_snapshot(s)
            existing = {t.name: t for t in s.scalars(select(Topic)).all()}
            for name, cfg in wanted.items():
                row = existing.get(name)
                fields = dict(
                    description=cfg.description,
                    keywords=list(cfg.keywords),
                    exclude_keywords=list(cfg.exclude_keywords),
                    categories=list(cfg.categories),
                    authors=list(cfg.authors),
                    quota=cfg.quota,
                    threshold=cfg.threshold,
                    enabled=cfg.enabled,
                )
                if row is None:
                    s.add(Topic(name=name, **fields))
                else:
                    for key, value in fields.items():
                        setattr(row, key, value)
            for name, row in existing.items():
                if name not in wanted:
                    s.delete(row)
            # 记录仪纪律：只记**真实发生**的状态变更（无变化不记，避免噪声）
            after_snapshot = _topics_to_dicts(wanted.values())
            if after_snapshot != before_snapshot:
                self._event(
                    s,
                    op="sync_topics",
                    actor=actor,
                    reason=reason,
                    target="topics",
                    before={"topics": before_snapshot},
                    after={"topics": after_snapshot},
                )
            s.commit()
            return list(s.scalars(select(Topic).where(Topic.name.in_(wanted.keys()))))

    @staticmethod
    def _topic_snapshot(s: Session) -> list[dict]:
        """当前 DB 主题的可序列化快照（undo 用）。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "keywords": list(t.keywords or []),
                "exclude_keywords": list(t.exclude_keywords or []),
                "categories": list(t.categories or []),
                "authors": list(t.authors or []),
                "quota": t.quota,
                "threshold": t.threshold,
                "enabled": t.enabled,
            }
            for t in s.scalars(select(Topic).order_by(Topic.id))
        ]

    def _apply_topic_snapshot(self, s: Session, topics: list[dict]) -> None:
        """把主题快照写回 DB（undo sync_topics 用）。"""
        wanted = {t["name"]: t for t in topics}
        existing = {t.name: t for t in s.scalars(select(Topic)).all()}
        for name, fields in wanted.items():
            row = existing.get(name)
            if row is None:
                s.add(Topic(name=name, **{k: v for k, v in fields.items() if k != "name"}))
            else:
                for key, value in fields.items():
                    if key != "name":
                        setattr(row, key, value)
        for name, row in existing.items():
            if name not in wanted:
                s.delete(row)

    def enabled_topics(self) -> list[Topic]:
        with self.sf() as s:
            return list(
                s.scalars(select(Topic).where(Topic.enabled.is_(True)).order_by(Topic.id))
            )

    # ---------------------------------------------------------------- papers
    def upsert_papers(
        self, items: Iterable[NormalizedPaper], *, actor: str = "system", reason: str = ""
    ) -> dict[str, int]:
        new = updated = 0
        with self.sf() as s:
            for item in items:
                row = s.scalar(select(Paper).where(Paper.arxiv_id == item.arxiv_id))
                if row is None:
                    row = Paper(
                        arxiv_id=item.arxiv_id,
                        version=item.version,
                        title=item.title,
                        abstract=item.abstract,
                        authors=list(item.authors),
                        categories=list(item.categories),
                        primary_category=item.primary_category,
                        published_at=item.published_at,
                        updated_at=item.updated_at,
                        pdf_url=item.pdf_url,
                        abs_url=item.abs_url,
                        status="new",
                        first_seen_at=utcnow(),
                    )
                    s.add(row)
                    s.flush()
                    self._index(s, row)
                    new += 1
                elif item.version > row.version:
                    row.version = item.version
                    row.title = item.title
                    row.abstract = item.abstract
                    row.authors = list(item.authors)
                    row.categories = list(item.categories)
                    row.primary_category = item.primary_category
                    row.published_at = item.published_at
                    row.updated_at = item.updated_at
                    row.pdf_url = item.pdf_url
                    row.abs_url = item.abs_url
                    s.flush()
                    self._index(s, row)
                    updated += 1
            if new or updated:
                # 入库不可逆（撤了论文，历史简报/打分就悬空）——标 reversible=0
                self._event(
                    s,
                    op="upsert_papers",
                    actor=actor,
                    reason=reason,
                    target="papers",
                    after={"new": new, "updated": updated},
                    reversible=0,
                )
            s.commit()
        return {"new": new, "updated": updated}

    def _index(self, session: Session, paper: Paper) -> None:
        if self.index is not None:
            self.index.upsert(
                session,
                paper_id=paper.id,
                title=paper.title,
                abstract=paper.abstract,
            )

    def candidates_for_topic(self, topic: Topic, *, lookback_days: int) -> list[Paper]:
        cutoff = utcnow() - timedelta(days=max(0, lookback_days))
        stmt = select(Paper).where(Paper.status == "new", Paper.first_seen_at >= cutoff)
        if topic.categories:
            stmt = stmt.where(Paper.primary_category.in_(list(topic.categories)))
        stmt = stmt.order_by(Paper.published_at.desc().nullslast(), Paper.id.desc())
        with self.sf() as s:
            return list(s.scalars(stmt))

    def mark_status(
        self, paper: Paper, status: str, *, actor: str = "system", reason: str = ""
    ) -> None:
        with self.sf() as s:
            row = s.get(Paper, paper.id)
            if row is not None and row.status != status:
                before = {"arxiv_id": row.arxiv_id, "status": row.status}
                row.status = status
                self._event(
                    s,
                    op="mark_status",
                    actor=actor,
                    reason=reason,
                    target=row.arxiv_id,
                    before=before,
                    after={"arxiv_id": row.arxiv_id, "status": status},
                )
                s.commit()

    def reopen_by_status(self, *, from_statuses: tuple[str, ...],
                         to_status: str = "new",
                         within_days: int | None = None,
                         actor: str = "human", reason: str = "") -> dict:
        """按状态批量回退（再审回炉：archived/in_briefing → new）。

        不重复写事件——先查 ids，再委托已有的 `reset_statuses(arxiv_ids, status, ...)`
        （它写的 `op=reset_statuses` 事件已接上 `_restore` 的现成分支，能一键 undo）。
        `within_days` 限缩到 `first_seen_at >= utcnow - N`：不把远古存量也拉回洗劫新
        一轮评审。空匹配 ⇒ 不产生事件。
        """
        cutoff = utcnow() - timedelta(days=within_days) if within_days else None
        with self.sf() as s:
            stmt = select(Paper.arxiv_id).where(Paper.status.in_(list(from_statuses)))
            if cutoff is not None:
                stmt = stmt.where(Paper.first_seen_at >= cutoff)
            ids = list(s.scalars(stmt).all())
        if not ids:
            return {"ok": True, "flipped": 0}
        flipped = self.reset_statuses(ids, status=to_status, actor=actor, reason=reason)
        return {"ok": True, "flipped": int(flipped)}

    def record_signal(self, arxiv_id: str, signal: str, *, source: str = "measured",
                      actor: str = "human", reason: str = "") -> None:
        """M0/M1 漏斗信号：事件留痕 + 画像权重增量（同事务）。

        信号是轻量人类操作，与 delete_note 同族（不与 AI 争写、自身不可回滚）；
        `signal:*` 进事件总线可审计，画像 bump 与事件同提交。
        """
        with self.sf() as s:
            self._event(
                s,
                op=f"signal:{signal}",
                actor=actor,
                reason=reason,
                target=arxiv_id,
                after={"arxiv_id": arxiv_id, "signal": signal, "source": source},
                reversible=0,
            )
            paper = s.scalar(select(Paper).where(Paper.arxiv_id == arxiv_id))
            if paper is not None:
                self._bump_profile(s, paper, signal)
            s.commit()

    # ---------------------------------------------------------------- M1 画像（信号→权重，衰减在读侧）
    SIGNAL_WEIGHTS = {"view": 0.3, "outbound": 0.5, "download": 1.0,
                      "star": 0.8, "read": 0.6, "skip": -1.0,
                      "uninterested": -1.5, "seed": 0.5}
    POS_CAP = 0.3        # 单事件单键正向限幅（防回音室失控；负向不放大）

    def _bump_profile(self, s: Session, paper: Paper, signal: str) -> None:
        """信号→三维权重增量（category 主1.0/副0.6 · term 0.5 · author 0.8）。"""
        from ..domain.profile import paper_features
        from .orm import ProfileWeight
        coef = self.SIGNAL_WEIGHTS.get(signal)
        if coef is None:
            return
        feat = paper_features(list(paper.categories or []), paper.primary_category,
                              paper.title or "", paper.abstract or "",
                              list(paper.authors or []))
        increments: dict[tuple[str, str], float] = {}
        for c in feat["categories"][:4]:
            increments[("category", c)] = coef * (1.0 if c == feat["primary"] else 0.6)
        for t in feat["terms"]:
            increments[("term", t)] = coef * 0.5
        for a in feat["authors"]:
            increments[("author", a)] = coef * 0.8
        for (kind, key), delta in increments.items():
            row = s.scalar(select(ProfileWeight).where(
                ProfileWeight.kind == kind, ProfileWeight.key == key))
            if row is None:
                row = ProfileWeight(kind=kind, key=key, w=0.0, hits=0)
                s.add(row)
            if delta > 0:
                delta = min(delta, self.POS_CAP)
            row.w += delta
            row.hits += 1
            row.updated_at = utcnow()

    def profile_view(self, *, top: int = 12, half_life_days: float = 30.0,
                     now: datetime | None = None) -> dict:
        """画像读数：top 权重 + 分类熵（防茧房哨兵）；衰减读侧计算，不改写库。"""
        import math

        from .orm import ProfileWeight
        now = now or utcnow()
        hl = max(0.001, float(half_life_days))
        agg: dict[str, list] = {"category": [], "term": [], "author": []}
        total_hits = 0
        with self.sf() as s:
            for r in s.scalars(select(ProfileWeight)).all():
                age = max(0.0, (now - r.updated_at).total_seconds() / 86400.0)
                w = r.w * (2.0 ** (-age / hl))
                agg.setdefault(r.kind, []).append([r.key, round(w, 4), r.hits])
                total_hits += r.hits
        top_lists = {k: sorted(v, key=lambda x: -x[1])[:top] for k, v in agg.items()}
        pos = [w for _, w, _ in agg["category"] if w > 0]
        tot = sum(pos)
        ent = (-sum((p / tot) * math.log2(p / tot) for p in pos)
               if tot > 0 else 0.0)
        return {"top": top_lists, "category_entropy": round(ent, 4),
                "total_hits": total_hits, "half_life_days": hl,
                "distinct_categories": sum(1 for _, w, _ in agg["category"]
                                            if abs(w) > 1e-9)}

    def profile_weights_map(self, *, half_life_days: float = 30.0,
                            now: datetime | None = None) -> dict:
        """(kind,key)→衰减后权重，供 M2 打分器/测试消费。"""
        from .orm import ProfileWeight
        now = now or utcnow()
        hl = max(0.001, float(half_life_days))
        out: dict[tuple[str, str], float] = {}
        with self.sf() as s:
            for r in s.scalars(select(ProfileWeight)).all():
                age = max(0.0, (now - r.updated_at).total_seconds() / 86400.0)
                out[(r.kind, r.key)] = r.w * (2.0 ** (-age / hl))
        return out

    def profile_seed_if_empty(self, topics: Sequence, *, actor: str = "system") -> int:
        """空画像 ⇒ 以 YAML 主题为先验播种（一次性；主题此后是种子不是门）。"""
        from .orm import ProfileWeight
        with self.sf() as s:
            if s.scalar(select(ProfileWeight.id)) is not None:
                return 0
            seeded = 0
            wanted: dict[tuple[str, str], float] = {}
            for t in topics:
                plan = [("category", list(getattr(t, "categories", []) or [])),
                        ("term", [k.lower() for k in (getattr(t, "keywords", []) or [])]),
                        ("author", list(getattr(t, "authors", []) or []))]
                for kind, keys in plan:
                    for key in keys:
                        if key:
                            # 多主题共享同一分类/词只能铸一行（(kind,key) 唯一约束）
                            wanted.setdefault((kind, key), self.SIGNAL_WEIGHTS["seed"])
            for (kind, key), w0 in wanted.items():
                s.add(ProfileWeight(kind=kind, key=key, w=w0, hits=0))
                seeded += 1
            if seeded:
                self._event(s, op="profile_seed", actor=actor,
                            reason="空画像播种：以 YAML 主题为先验",
                            target="profile", after={"seeded": seeded}, reversible=0)
            s.commit()
            return seeded

    def profile_reset(self, *, kind: str = "", actor: str = "human",
                      reason: str = "") -> dict:
        """清空画像（整表或按 kind）。before 快照 ⇒ undo 可还原（中毒重来不丢历史）。"""
        from .orm import ProfileWeight
        with self.sf() as s:
            stmt = select(ProfileWeight)
            if kind:
                stmt = stmt.where(ProfileWeight.kind == kind)
            rows = list(s.scalars(stmt).all())
            snap = [{"kind": r.kind, "key": r.key, "w": r.w, "hits": r.hits} for r in rows]
            for r in rows:
                s.delete(r)
            if snap:
                self._event(s, op="profile_reset", actor=actor,
                            reason=reason or "画像重置（中毒/冷启动重来）",
                            target=kind or "all",
                            before={"rows": snap}, after={"removed": len(snap)},
                            reversible=1)
            s.commit()
            return {"ok": True, "removed": len(snap), "kind": kind or "all"}

    def record_op(self, op: str, *, target: str = "", after: dict | None = None,
                  actor: str = "system", reason: str = "") -> None:
        """操作留痕（只读面的使用日志，如 feed 刷新）：进归因总线，reversible=0。

        读操作不改状态不进 mecha History（监控面按设计只显状态变化），但**谁在刷、
        刷出了什么**属于域归因面，该进 Web /activity 记录仪。
        """
        with self.sf() as s:
            self._event(s, op=op, actor=actor, reason=reason, target=target,
                        after=after or {}, reversible=0)
            s.commit()

    def feed_candidates(self, *, days: int, limit: int = 800) -> list[Paper]:
        """近 N 天 published_at 的库内论文，发布日倒序（feed 候选池）。

        注：既有 `recent_papers` 是论文库列表页用的（无日期窗口），两码事不同名。
        """
        from datetime import timedelta
        cutoff = utcnow() - timedelta(days=max(1, int(days)))
        with self.sf() as s:
            return list(s.scalars(select(Paper)
                                  .where(Paper.published_at >= cutoff)
                                  .order_by(Paper.published_at.desc())
                                  .limit(int(limit))).all())

    def feed_seen_ids(self, *, days: int = 7) -> set[str]:
        """近 N 天有过信号（view/outbound/download/uninterested…）的 arxiv_id——feed 换屏不重喂。"""
        if days <= 0:
            return set()
        from datetime import timedelta

        from .orm import Event
        cutoff = utcnow() - timedelta(days=int(days))
        with self.sf() as s:
            rows = s.scalars(select(Event.target).where(
                Event.op.like("signal:%"), Event.ts >= cutoff)).all()
        return {r for r in rows if r}

    # ---------------------------------------------------------------- feed 期票（AI 发布，面板只读期）
    def write_summary(self, arxiv_id: str, *, summary: dict, score: dict | None,
                      actor: str = "ai", reason: str = "") -> dict | None:
        """AI 单篇补卡：写最新总结行（+可选评分行），返回 {run_id}；不在库回 None。

        run_id 用独立的 "card-<hex8>"，不挂任何流水线 run——补卡与日报是两类动作、
        审计分开；卡片只此一份真相，/feed 卡、详情页、read_paper 经 latest_summary 自动复用。
        reversible=1：undo 删掉这一轮的两行。
        """
        import uuid

        from .orm import PaperScore, PaperSummaryRow
        run_id = "card-" + uuid.uuid4().hex[:8]
        with self.sf() as s:
            paper = s.scalar(select(Paper).where(Paper.arxiv_id == arxiv_id))
            if paper is None:
                return None
            s.add(PaperSummaryRow(
                run_id=run_id, paper_id=paper.id,
                tldr=summary.get("tldr", ""), problem=summary.get("problem", ""),
                method=summary.get("method", ""), results=summary.get("results", ""),
                novelty=summary.get("novelty", ""),
                keywords=list(summary.get("keywords") or []),
                model=summary.get("model", "dsh-card")))
            if score is not None:
                s.add(PaperScore(run_id=run_id, paper_id=paper.id, topic_id=None,
                                 score=float(score["score"]), label=score["label"],
                                 reason=score.get("reason", ""), tags=[],
                                 model=score.get("model", "dsh-card")))
            self._event(s, op="write_summary", actor=actor,
                        reason=reason or f"AI 补卡：{arxiv_id}", target=arxiv_id,
                        after={"run_id": run_id, "has_score": score is not None,
                               "label": (score or {}).get("label", "")},
                        reversible=1)
            s.commit()
            return {"run_id": run_id}

    # ---------------------------------------------------------------- 引文网络与调研统计（M4）
    def replace_citation_edges(self, src_arxiv_id: str, rows: list[dict], *,
                               actor: str = "ai", reason: str = "") -> dict:
        """src 的引用边全集重跑：删旧插新幂等；旧边快照进事件，undo 可回。"""
        from .orm import CitationEdge
        with self.sf() as s:
            old = list(s.scalars(select(CitationEdge).where(
                CitationEdge.src_arxiv_id == src_arxiv_id)).all())
            snapshot = [{"dst": e.dst_arxiv_id, "title": e.dst_title,
                         "citations": e.dst_citations, "infl": e.influential,
                         "year": e.year} for e in old]
            for e in old:
                s.delete(e)
            s.flush()                       # 先落删除，同 (src,dst) 重跑不撞唯一约束
            added = 0
            for r in rows:
                dst = (r.get("arxiv_id") or "").strip()
                if not dst or dst == src_arxiv_id:
                    continue
                s.add(CitationEdge(
                    src_arxiv_id=src_arxiv_id, dst_arxiv_id=dst,
                    dst_title=r.get("title") or "",
                    dst_citations=int(r.get("citation_count") or 0),
                    influential=bool(r.get("influential")),
                    direction="cites",
                    year=int(r.get("year") or 0)))
                added += 1
            self._event(s, op="sync_citations", actor=actor,
                        reason=reason or f"落库引文边：{src_arxiv_id}", target=src_arxiv_id,
                        before={"edges": snapshot},
                        after={"added": added, "replaced": len(old)}, reversible=1)
            s.commit()
            return {"added": added, "replaced": len(old)}

    def citation_edges_all(self, *, limit: int = 1500) -> list:
        """全边列表（web 网络页数据源；项目已有先例：briefings 返离 session 实体）。"""
        from .orm import CitationEdge
        with self.sf() as s:
            return list(s.scalars(select(CitationEdge)
                                  .order_by(CitationEdge.dst_citations.desc())
                                  .limit(int(limit))).all())

    def upstream_clusters(self, *, min_count: int = 2, limit: int = 20) -> list[dict]:
        """关键上游簇：库内多篇反复引用的同一文献（入组数≥min_count），按组数降序。"""
        from .orm import CitationEdge
        with self.sf() as s:
            rows = list(s.scalars(select(CitationEdge)).all())
        grouped: dict[str, dict] = {}
        for e in rows:
            g = grouped.setdefault(e.dst_arxiv_id, {
                "dst_arxiv_id": e.dst_arxiv_id, "title": e.dst_title,
                "dst_citations": e.dst_citations, "cited_by": []})
            g["cited_by"].append(e.src_arxiv_id)
        out = [g for g in grouped.values() if len(g["cited_by"]) >= max(2, int(min_count))]
        out.sort(key=lambda g: (-len(g["cited_by"]), -g["dst_citations"]))
        for g in out:
            g["count"] = len(g["cited_by"])
            g["cited_by"] = g["cited_by"][:8]
        return out[:limit]

    def related_by_cocitation(self, arxiv_id: str, *, limit: int = 10) -> list[dict]:
        """共引相似：与本篇引用集交集最大的库内其它论文（邻接道的免费弹药）。"""
        from .orm import CitationEdge
        with self.sf() as s:
            mine = {e.dst_arxiv_id for e in s.scalars(select(CitationEdge).where(
                CitationEdge.src_arxiv_id == arxiv_id)).all()}
            others = list(s.scalars(select(CitationEdge).where(
                CitationEdge.src_arxiv_id != arxiv_id)).all())
        if not mine:
            return []
        inter: dict[str, set] = {}
        for e in others:
            inter.setdefault(e.src_arxiv_id, set()).add(e.dst_arxiv_id)
        scored = sorted(((len(mine & dsts), src) for src, dsts in inter.items() if mine & dsts),
                        key=lambda x: (-x[0], x[1]))
        return [{"arxiv_id": src, "shared_references": n} for n, src in scored[:limit]]

    # ---------------------------------------------------------------- 图论标签与反向边（P2/P4）
    TAGS = ("平台源头", "理论源头", "综述枢纽", "实验谱系", "下游扩散", "动机")

    def set_tag(self, arxiv_id: str, tag: str, *, actor: str = "ai",
                reason: str = "") -> dict:
        """一论文一枚标签；重跑替换。before=旧标 ⇒ undo 还原旧标或删行。"""
        from .orm import PaperTag
        with self.sf() as s:
            old = s.get(PaperTag, arxiv_id)
            snap = old.tag if old is not None else ""
            if old is not None:
                old.tag, old.actor, old.ts = tag, actor, utcnow()
            else:
                s.add(PaperTag(arxiv_id=arxiv_id, tag=tag, actor=actor))
            self._event(s, op="tag_paper", actor=actor,
                        reason=reason or f"给 {arxiv_id} 标 {tag}", target=arxiv_id,
                        before={"tag": snap}, after={"tag": tag}, reversible=1)
            s.commit()
            return {"tag": tag, "replaced": bool(snap)}

    def tag_map(self) -> dict[str, str]:
        from .orm import PaperTag
        with self.sf() as s:
            return {t.arxiv_id: t.tag for t in s.scalars(select(PaperTag)).all()}

    def tag_counts(self) -> dict[str, int]:
        """标签 → 篇数（图例与批量钉标的回执）。"""
        from .orm import PaperTag
        with self.sf() as s:
            out: dict[str, int] = {}
            for t in s.scalars(select(PaperTag)).all():
                out[t.tag] = out.get(t.tag, 0) + 1
            return out

    def set_tags(self, rows: list[dict], *, actor: str = "ai", reason: str = "") -> dict:
        """**批量**钉标（一次事件、可整批撤销）：rows=[{arxiv_id, tag}]。

        单篇一枚的语义不变（重跑替换）；批量入口只是把 N 次调用收成 1 次——
        此前给四十篇钉标要四十条事件，审计与撤销都难用。
        """
        from .orm import PaperTag
        with self.sf() as s:
            before: list[dict] = []
            applied: list[dict] = []
            for r in rows:
                aid = (r.get("arxiv_id") or "").strip()
                tag = (r.get("tag") or "").strip()
                if not aid or not tag:
                    continue
                old = s.get(PaperTag, aid)
                before.append({"arxiv_id": aid, "tag": old.tag if old is not None else ""})
                if old is not None:
                    old.tag, old.actor, old.ts = tag, actor, utcnow()
                else:
                    s.add(PaperTag(arxiv_id=aid, tag=tag, actor=actor))
                applied.append({"arxiv_id": aid, "tag": tag})
            if applied:
                self._event(s, op="set_tags", actor=actor,
                            reason=reason or f"批量钉标 {len(applied)} 篇",
                            target=f"tags:{len(applied)}",
                            before={"tags": before}, after={"tags": applied}, reversible=1)
            s.commit()
            return {"applied": applied, "count": len(applied)}

    # ---------------------------------------------------------------- 图视图（P5：视图一等公民）
    def save_graph_view(self, name: str, spec: dict, *, is_default: bool = True,
                        actor: str = "ai", reason: str = "") -> dict:
        """发布/更新一张视图（name 为键，重跑替换）；``is_default`` 时把默认指针挪过来。

        视图 = 根/深度/布局/分组/着色/标签/预算/锚点 的一份 spec：HTML 渲染与
        /network.json 读同一份 ⇒ "AI 画的"就是"页面显示的"。
        """
        from .orm import GraphView
        name = (name or "").strip() or "默认视图"
        with self.sf() as s:
            old = s.get(GraphView, name)
            snapshot = ({"existed": True, "spec": old.spec or {}, "actor": old.actor,
                         "reason": old.reason, "is_default": bool(old.is_default)}
                        if old is not None else {"existed": False})
            if old is None:
                s.add(GraphView(name=name, spec=dict(spec), is_default=bool(is_default),
                                actor=actor, reason=reason))
            else:
                old.spec, old.actor, old.reason = dict(spec), actor, reason
                old.is_default, old.ts = bool(is_default), utcnow()
            if is_default:
                for v in s.scalars(select(GraphView)).all():
                    if v.name != name:
                        v.is_default = False
            self._event(s, op="set_graph_view", actor=actor,
                        reason=reason or f"发布视图：{name}", target=name,
                        before=snapshot, after={"spec": dict(spec), "is_default": bool(is_default)},
                        reversible=1)
            s.commit()
            return {"name": name, "replaced": old is not None, "is_default": bool(is_default)}

    def get_graph_view(self, name: str) -> dict | None:
        from .orm import GraphView
        with self.sf() as s:
            row = s.get(GraphView, (name or "").strip())
            if row is None:
                return None
            return {"name": row.name, "spec": dict(row.spec or {}),
                    "is_default": bool(row.is_default), "ts": row.ts,
                    "actor": row.actor, "reason": row.reason}

    def default_graph_view(self) -> dict | None:
        from .orm import GraphView
        with self.sf() as s:
            row = s.scalars(select(GraphView).where(GraphView.is_default.is_(True))).first()
            if row is None:
                return None
            return {"name": row.name, "spec": dict(row.spec or {}),
                    "is_default": True, "ts": row.ts, "actor": row.actor,
                    "reason": row.reason}

    def list_graph_views(self) -> list[dict]:
        from .orm import GraphView
        with self.sf() as s:
            rows = list(s.scalars(select(GraphView)).all())
        rows.sort(key=lambda r: (not r.is_default, r.name))
        return [{"name": r.name, "is_default": bool(r.is_default), "spec": dict(r.spec or {}),
                 "ts": r.ts, "actor": r.actor, "reason": r.reason} for r in rows]

    def set_default_graph_view(self, name: str, *, actor: str = "ai",
                               reason: str = "") -> dict:
        from .orm import GraphView
        name = (name or "").strip()
        with self.sf() as s:
            target = s.get(GraphView, name)
            if target is None:
                raise AIError(f"没有这张视图：{name}", kind="not_found",
                              hint="先用 query_graph_views 看已发布的视图名")
            prev = next((v.name for v in s.scalars(select(GraphView)).all()
                         if v.is_default), "")
            for v in s.scalars(select(GraphView)).all():
                v.is_default = v.name == name
            self._event(s, op="set_default_view", actor=actor,
                        reason=reason or f"默认视图切到 {name}", target=name,
                        before={"default": prev}, after={"default": name}, reversible=1)
            s.commit()
            return {"name": name, "previous": prev}

    def delete_graph_view(self, name: str, *, actor: str = "ai", reason: str = "") -> dict:
        from .orm import GraphView
        name = (name or "").strip()
        with self.sf() as s:
            row = s.get(GraphView, name)
            if row is None:
                raise AIError(f"没有这张视图：{name}", kind="not_found",
                              hint="先用 query_graph_views 看已发布的视图名")
            snap = {"existed": True, "spec": row.spec or {}, "actor": row.actor,
                    "reason": row.reason, "is_default": bool(row.is_default)}
            s.delete(row)
            self._event(s, op="set_graph_view", actor=actor,
                        reason=reason or f"删除视图：{name}", target=name,
                        before=snap, after={"deleted": True}, reversible=1)
            s.commit()
            return {"deleted": name}

    def edge_years(self) -> dict[str, int]:
        """dst → 年份（年代编排用）：同篇多来源取最大值（新近者优先）。"""
        from .orm import CitationEdge
        with self.sf() as s:
            rows = s.scalars(select(CitationEdge)).all()
        out: dict[str, int] = {}
        for e in rows:
            y = int(e.year or 0)
            for aid in (e.dst_arxiv_id, e.src_arxiv_id):
                if y and y > out.get(aid, 0):
                    out[aid] = y
        return out

    def add_citation_edges(self, rows: list[dict], *, actor: str = "ai",
                           reason: str = "", target: str = "") -> int:
        """增量幂等加边（P4 反向边用）：已存在的 (src,dst) 跳过。
        增量加边标 reversible=0——整篇出边重建用 sync_citations（快照替换语义）。"""
        from .orm import CitationEdge
        with self.sf() as s:
            have = {(e.src_arxiv_id, e.dst_arxiv_id)
                    for e in s.scalars(select(CitationEdge)).all()}
            added = 0
            for r in rows:
                src = (r.get("src") or "").strip()
                dst = (r.get("dst") or "").strip()
                if not src or not dst or src == dst or (src, dst) in have:
                    continue
                s.add(CitationEdge(src_arxiv_id=src, dst_arxiv_id=dst,
                                   dst_title=r.get("title") or dst,
                                   dst_citations=int(r.get("citations") or 0),
                                   influential=bool(r.get("influential")),
                                   direction="cited_by",
                                   year=int(r.get("year") or 0)))
                have.add((src, dst))
                added += 1
            self._event(s, op="sync_cited_by", actor=actor,
                        reason=reason or f"反向补边 {added} 条", target=target,
                        after={"added": added}, reversible=0)
            s.commit()
            return added

    def coverage_report(self, *, sample_missing: int = 15) -> dict:
        """调研资产覆盖率：多少篇有卡/读过/收藏，最新的无卡清单（AI 补卡工单）。"""
        from .orm import PaperSummaryRow, ReadingState
        with self.sf() as s:
            papers = list(s.scalars(select(Paper).order_by(Paper.first_seen_at.desc())).all())
            with_sum = set(s.scalars(select(PaperSummaryRow.paper_id)).all())
            reading = {r.paper_id: r for r in s.scalars(select(ReadingState)).all()}
        total = len(papers)
        has_sum = sum(1 for p in papers if p.id in with_sum)
        missing = [{"arxiv_id": p.arxiv_id, "title": (p.title or "")[:120]}
                   for p in papers if p.id not in with_sum][:sample_missing]
        return {
            "total": total, "with_summary": has_sum,
            "coverage_pct": round(100.0 * has_sum / total, 1) if total else 0.0,
            "read": sum(1 for p in papers if p.id in reading and reading[p.id].read),
            "starred": sum(1 for p in papers if p.id in reading and reading[p.id].star),
            "in_briefing": sum(1 for p in papers if p.status == "in_briefing"),
            "missing_sample": missing,
        }

    def stats_timeseries(self, *, days: int = 30) -> dict:
        """趋势聚合：每日入库、信号漏斗计数、简报节奏、AI 成本按 purpose（近 N 天）。"""
        from collections import defaultdict
        from datetime import timedelta

        from .orm import AICall
        cutoff = utcnow() - timedelta(days=max(1, int(days)))
        with self.sf() as s:
            daily_new: dict[str, int] = defaultdict(int)
            for ts in s.scalars(select(Paper.first_seen_at)).all():
                if ts and ts >= cutoff:
                    daily_new[ts.date().isoformat()] += 1
            signals: dict[str, int] = defaultdict(int)
            for op, in s.execute(select(Event.op).where(
                    Event.op.like("signal:%"), Event.ts >= cutoff)).all():
                signals[op] += 1
            brief_days = [b.date for b in s.scalars(select(Briefing).where(
                Briefing.status != "superseded", Briefing.created_at >= cutoff)).all()]
            ai_agg: dict[str, dict] = {}
            for c in s.scalars(select(AICall).where(AICall.ts >= cutoff)).all():
                a = ai_agg.setdefault(c.purpose or "?", {"calls": 0, "ok": 0, "tokens": 0, "latency_ms": 0})
                a["calls"] += 1
                a["ok"] += 1 if c.ok else 0
                a["tokens"] += int(c.tokens or 0)
                a["latency_ms"] += int(c.latency_ms or 0)
        for a in ai_agg.values():
            a["avg_latency_ms"] = round(a["latency_ms"] / max(1, a["calls"]))
            a.pop("latency_ms")
        return {
            "days": days,
            "daily_new": dict(sorted(daily_new.items())),
            "signals": dict(sorted(signals.items())),
            "briefings": {"days_with_briefing": len(set(brief_days)),
                          "total": len(brief_days)},
            "ai_by_purpose": ai_agg,
        }

    def save_feed_issue(self, *, params: dict, items: list, actor: str = "ai",
                        reason: str = "") -> int:
        """存一期 feed 快照 + 事件（op=save_feed，可撤销：删回这期）。"""
        from .orm import FeedIssue
        with self.sf() as s:
            row = FeedIssue(params=params, items=items, actor=actor, reason=reason)
            s.add(row)
            s.flush()
            self._event(s, op="save_feed", actor=actor,
                        reason=reason or "发布 feed 一期",
                        target=str(row.id),
                        after={"id": row.id, "count": len(items), "params": params},
                        reversible=1)
            s.commit()
            return int(row.id)

    def latest_feed_issue(self):
        """最新一期（无期回 None）；/feed 面板默认只读它。"""
        from .orm import FeedIssue
        with self.sf() as s:
            return s.scalars(select(FeedIssue)
                             .order_by(FeedIssue.id.desc()).limit(1)).first()

    def feed_issue_count(self) -> int:
        from .orm import FeedIssue
        with self.sf() as s:
            return len(s.scalars(select(FeedIssue.id)).all())

    def reset_statuses(
        self,
        arxiv_ids: Sequence[str],
        status: str = "new",
        *,
        actor: str = "system",
        reason: str = "",
    ) -> int:
        """把指定论文重置为某状态（demo 重放 / 手动翻案用）。"""
        ids = list(arxiv_ids)
        if not ids:
            return 0
        count = 0
        with self.sf() as s:
            rows = list(s.scalars(select(Paper).where(Paper.arxiv_id.in_(ids))))
            before_items = [
                {"arxiv_id": row.arxiv_id, "status": row.status} for row in rows
            ]
            for row in rows:
                if row.status != status:
                    row.status = status
                    count += 1
            if count:
                self._event(
                    s,
                    op="reset_statuses",
                    actor=actor,
                    reason=reason,
                    target="papers",
                    before={"items": before_items},
                    after={"items": [{"arxiv_id": r.arxiv_id, "status": status} for r in rows]},
                )
            s.commit()
        return count

    def get_paper(self, arxiv_id: str) -> Paper | None:
        with self.sf() as s:
            stmt = (
                select(Paper)
                .options(*_PAPER_EAGER)
                .where(Paper.arxiv_id == arxiv_id)
            )
            return s.scalars(stmt).first()

    def recent_papers(self, *, limit: int = 50, status: str | None = None) -> list[Paper]:
        stmt = select(Paper).options(*_PAPER_EAGER)
        if status:
            stmt = stmt.where(Paper.status == status)
        stmt = stmt.order_by(Paper.published_at.desc().nullslast(), Paper.id.desc()).limit(limit)
        with self.sf() as s:
            return list(s.scalars(stmt))

    def counts_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self.sf() as s:
            rows = s.execute(
                select(Paper.status, func.count()).group_by(Paper.status)
            )
            for status, count in rows:
                out[str(status)] = int(count)
        return out

    # ---------------------------------------------------------------- scores / summaries
    def save_scores(
        self,
        *,
        run_id: str,
        paper: Paper,
        topic_id: int | None,
        score: RelevanceScore,
        model: str,
    ) -> None:
        with self.sf() as s:
            exists = s.scalar(
                select(PaperScore).where(
                    PaperScore.run_id == run_id, PaperScore.paper_id == paper.id
                )
            )
            if exists is not None:
                return
            s.add(
                PaperScore(
                    run_id=run_id,
                    paper_id=paper.id,
                    topic_id=topic_id,
                    score=score.score,
                    label=score.label,
                    reason=score.reason,
                    tags=list(score.tags),
                    model=model,
                )
            )
            s.commit()

    def latest_scores(self, paper: Paper, *, limit: int = 5) -> list[PaperScore]:
        with self.sf() as s:
            return list(
                s.scalars(
                    select(PaperScore)
                    .where(PaperScore.paper_id == paper.id)
                    .order_by(PaperScore.id.desc())
                    .limit(limit)
                )
            )

    def save_summary(
        self,
        *,
        run_id: str,
        paper: Paper,
        summary: PaperSummary,
        model: str,
        tokens: int = 0,
        latency_ms: int = 0,
    ) -> None:
        with self.sf() as s:
            exists = s.scalar(
                select(PaperSummaryRow).where(
                    PaperSummaryRow.run_id == run_id, PaperSummaryRow.paper_id == paper.id
                )
            )
            if exists is not None:
                return
            s.add(
                PaperSummaryRow(
                    run_id=run_id,
                    paper_id=paper.id,
                    tldr=summary.tldr,
                    problem=summary.problem,
                    method=summary.method,
                    results=summary.results,
                    novelty=summary.novelty,
                    keywords=list(summary.keywords),
                    model=model,
                    tokens=tokens,
                    latency_ms=latency_ms,
                )
            )
            s.commit()
            if self.index is not None:
                # 同步刷新全文索引里的 tldr / keywords
                self.index.upsert(
                    s,
                    paper_id=paper.id,
                    title=paper.title or "",
                    abstract=paper.abstract or "",
                    tldr=summary.tldr,
                    keywords=summary.keywords,
                )
                s.commit()

    def latest_summary(self, paper: Paper) -> PaperSummaryRow | None:
        with self.sf() as s:
            return s.scalar(
                select(PaperSummaryRow)
                .where(PaperSummaryRow.paper_id == paper.id)
                .order_by(PaperSummaryRow.id.desc())
            )

    # ---------------------------------------------------------------- briefings / runs
    def save_briefing(
        self,
        *,
        date: str,
        run_id: str,
        title: str,
        markdown: str,
        stats: dict,
        ai_enabled: bool,
        actor: str = "system",
        reason: str = "",
    ) -> Briefing:
        with self.sf() as s:
            for old in s.scalars(select(Briefing).where(Briefing.date == date)).all():
                old.status = "superseded"
            briefing = Briefing(
                date=date,
                run_id=run_id,
                title=title,
                markdown=markdown,
                stats=stats,
                ai_enabled=ai_enabled,
                status="draft",
            )
            s.add(briefing)
            # 定稿不可逆（旧 briefing 已被标 superseded，回滚会丢历史）——reversible=0
            self._event(
                s,
                op="save_briefing",
                actor=actor,
                reason=reason,
                target=date,
                after={
                    "date": date,
                    "run_id": run_id,
                    "selected": len(stats.get("items", [])) if isinstance(stats, dict) else 0,
                },
                reversible=0,
            )
            s.commit()
            s.refresh(briefing)
            return briefing

    def briefing_for_date(self, date: str) -> Briefing | None:
        with self.sf() as s:
            return s.scalar(
                select(Briefing)
                .where(Briefing.date == date)
                .order_by(Briefing.id.desc())
            )

    def briefings(self, *, limit: int = 30) -> list[Briefing]:
        """当前简报列表：**一天只认一行**。

        `save_briefing` 重跑时会把旧行标 `superseded` 留在库里作历史；若把它们
        一起当条目端出去，同日就会"两篇日报"——日期高亮双份、管理表两行共命运
        （用户实测：同删同亮）。superseded 是内部历史，不是可管理对象。"""
        with self.sf() as s:
            return list(
                s.scalars(
                    select(Briefing)
                    .where(Briefing.status != "superseded")
                    .order_by(Briefing.date.desc())
                    .limit(limit)
                )
            )

    def delete_briefing(self, date: str, *, actor: str = "human",
                        reason: str = "") -> dict:
        """删除指定日期的简报（Web 管理 & 命令面共用）。可逆：`_restore` 重建行。

        同日多版本旧历史（`save_briefing` 会把旧行标 superseded）一并删——
        `briefing_for_date` 本就总取最新一行，保留历史不影响面板。
        快照写入 before 中存最新一行的 markdown+stats，供 undo 一键重建。
        """
        with self.sf() as s:
            rows = list(s.scalars(
                select(Briefing).where(Briefing.date == date)
            ).all())
            if not rows:
                return {"ok": True, "date": date, "deleted": 0, "already": True}
            latest = max(rows, key=lambda b: b.id)
            snap = {
                "date": latest.date, "run_id": latest.run_id,
                "title": latest.title, "markdown": latest.markdown,
                "stats": dict(latest.stats or {}),
                "ai_enabled": bool(latest.ai_enabled),
                "status": latest.status,
            }
            for r in rows:
                s.delete(r)
            self._event(
                s, op="delete_briefing", actor=actor, reason=reason,
                target=date, before=snap, after={"deleted": True},
                reversible=1,
            )
            s.commit()
        return {"ok": True, "date": date, "deleted": len(rows), "already": False}

    def create_run(self, run_id: str, *, mode: str = "daily") -> Run:
        run = Run(id=run_id, mode=mode, status="running")
        with self.sf() as s:
            s.add(run)
            s.commit()
        return run

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        stats: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self.sf() as s:
            run = s.get(Run, run_id)
            if run is None:
                return
            run.status = status
            run.finished_at = utcnow()
            if stats is not None:
                run.stats = stats
            if error is not None:
                run.error = error
            s.commit()

    def last_run(self) -> Run | None:
        with self.sf() as s:
            return s.scalar(select(Run).order_by(Run.started_at.desc()))

    # ---------------------------------------------------------------- ai 记账
    def log_ai_call(
        self,
        *,
        port: str,
        purpose: str,
        model: str,
        latency_ms: int = 0,
        tokens: int = 0,
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        with self.sf() as s:
            s.add(
                AICall(
                    port=port,
                    purpose=purpose,
                    model=model,
                    latency_ms=latency_ms,
                    tokens=tokens,
                    ok=ok,
                    error=error,
                )
            )
            s.commit()

    def ai_calls_since(self, since: datetime, *, limit: int = 100) -> list[AICall]:
        with self.sf() as s:
            return list(
                s.scalars(
                    select(AICall)
                    .where(AICall.ts >= since)
                    .order_by(AICall.ts.desc())
                    .limit(limit)
                )
            )

    # ---------------------------------------------------------------- 检索
    def search_papers(
        self,
        query: str,
        *,
        label: str | None = None,
        primary_category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Paper]:
        offset = max(0, int(offset))
        with self.sf() as s:
            stmt = select(Paper).options(*_PAPER_EAGER)
            if query and self.index is not None:
                # N4：取 limit+offset 再切窗口（FTS 层无 OFFSET，只能多取后页切）。
                ids = self.index.search(query, limit=limit + offset)[offset:offset + limit]
                if not ids:
                    return []
                stmt = stmt.where(Paper.id.in_(ids))
                papers = {p.id: p for p in s.scalars(stmt)}
                ordered = [papers[i] for i in ids if i in papers]
            else:
                if primary_category:
                    stmt = stmt.where(Paper.primary_category == primary_category)
                ordered = list(
                    s.scalars(
                        stmt.order_by(Paper.published_at.desc().nullslast(), Paper.id.desc())
                        .offset(offset).limit(limit)
                    )
                )
            if label:
                ordered = [p for p in ordered if p.scores and p.scores[-1].label == label]
            if primary_category and query:
                ordered = [p for p in ordered if p.primary_category == primary_category]
            return ordered

    # ---------------------------------------------------------------- 人工状态
    def _ensure_reading(self, s: Session, paper: Paper) -> ReadingState:
        row = s.get(ReadingState, paper.id)
        if row is None:
            row = ReadingState(paper_id=paper.id)
            s.add(row)
            s.flush()
        return row

    def _reading_event(
        self,
        s: Session,
        paper: Paper,
        row: ReadingState,
        *,
        op: str,
        actor: str,
        reason: str,
        before: dict,
    ) -> None:
        """before 必须由调用方在**变更前**拍好（否则 undo 回写的是新值）。"""
        self._event(
            s,
            op=op,
            actor=actor,
            reason=reason,
            target=paper.arxiv_id,
            before=before,
            after={"arxiv_id": paper.arxiv_id, **{f: bool(getattr(row, f)) for f in _READING_FIELDS}},
        )

    def _reading_snapshot(self, paper: Paper) -> dict:
        """读当前阅读态三元（含 arxiv_id）。"""
        with self.sf() as s:
            row = s.get(ReadingState, paper.id)
            triple = {f: bool(getattr(row, f)) for f in _READING_FIELDS} if row else {
                f: False for f in _READING_FIELDS
            }
        return {"arxiv_id": paper.arxiv_id, **triple}

    def set_read(self, paper: Paper, *, read: bool, actor: str = "system", reason: str = "") -> None:
        before = self._reading_snapshot(paper)
        with self.sf() as s:
            row = self._ensure_reading(s, paper)
            if row.read != read:
                row.read = read
                self._reading_event(
                    s, paper, row, op="set_read", actor=actor, reason=reason, before=before
                )
                s.commit()

    def toggle_star(self, paper: Paper, *, actor: str = "system", reason: str = "") -> bool:
        before = self._reading_snapshot(paper)
        with self.sf() as s:
            row = self._ensure_reading(s, paper)
            row.star = not row.star
            self._reading_event(
                s, paper, row, op="star_paper", actor=actor, reason=reason, before=before
            )
            s.commit()
            s.refresh(row)
            return bool(row.star)

    def set_marked_skip(
        self, paper: Paper, *, skip: bool, actor: str = "system", reason: str = ""
    ) -> None:
        before = self._reading_snapshot(paper)
        with self.sf() as s:
            row = self._ensure_reading(s, paper)
            if row.marked_skip != skip:
                row.marked_skip = skip
                self._reading_event(
                    s, paper, row, op="skip_paper", actor=actor, reason=reason, before=before
                )
                s.commit()

    def add_note(
        self, paper: Paper, content: str, *, actor: str = "system", reason: str = ""
    ) -> Note:
        with self.sf() as s:
            note = Note(paper_id=paper.id, content=content)
            s.add(note)
            s.flush()
            self._event(
                s,
                op="add_note",
                actor=actor,
                reason=reason,
                target=paper.arxiv_id,
                after={"note_id": note.id, "paper_id": paper.id, "content": content[:500]},
            )
            s.commit()
            s.refresh(note)
            return note

    def delete_note(self, note_id: int, *, actor: str = "system", reason: str = "") -> None:
        with self.sf() as s:
            note = s.get(Note, note_id)
            if note is not None:
                before = {
                    "note_id": note.id,
                    "paper_id": note.paper_id,
                    "content": note.content,
                }
                s.delete(note)
                self._event(
                    s,
                    op="delete_note",
                    actor=actor,
                    reason=reason,
                    target=str(note_id),
                    before=before,
                )
                s.commit()

    def notes_for(self, paper: Paper) -> list[Note]:
        with self.sf() as s:
            return list(
                s.scalars(
                    select(Note)
                    .where(Note.paper_id == paper.id)
                    .order_by(Note.id.desc())
                )
            )


def _topics_to_dicts(topics) -> list[dict]:
    """TopicCfg 序列化为可 JSON 快照（events.after 用）。"""
    return [
        {
            "name": t.name,
            "description": t.description,
            "keywords": list(t.keywords),
            "exclude_keywords": list(t.exclude_keywords),
            "categories": list(t.categories),
            "authors": list(t.authors),
            "quota": t.quota,
            "threshold": t.threshold,
            "enabled": t.enabled,
        }
        for t in topics
    ]
