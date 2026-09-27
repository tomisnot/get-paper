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

        raise AIError(
            f"操作 {op} 不支持撤销",
            kind="unsupported_undo",
            hint="目前支持：阅读态/笔记/主题同步/状态重置/删简报/画像重置的撤销",
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
