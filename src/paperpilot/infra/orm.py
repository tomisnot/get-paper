"""SQLAlchemy 实体（务实简化：实体即 ORM 模型，见 DESIGN.md §11 备注）。

schema 契约（唯一约束 = 幂等保证）：
- papers.arxiv_id           唯一 → 重复抓取只更新版本/字段
- (run_id, paper_id)        唯一 → 同一次运行重复打分/总结不会写两行
- briefings.date            逻辑唯一   → 同一天一份简报（重跑前旧版标 superseded）
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclude_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    authors: Mapped[list[str]] = mapped_column(JSON, default=list)
    quota: Mapped[int] = mapped_column(Integer, default=4)
    threshold: Mapped[float] = mapped_column(Float, default=0.6)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    arxiv_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(Text, default="")
    abstract: Mapped[str] = mapped_column(Text, default="")
    authors: Mapped[list[str]] = mapped_column(JSON, default=list)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    primary_category: Mapped[str] = mapped_column(String(32), default="", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pdf_url: Mapped[str] = mapped_column(String(500), default="")
    abs_url: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(32), default="arxiv")
    # new → rejected / in_briefing / archived / read
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    scores: Mapped[list[PaperScore]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="PaperScore.id"
    )
    summaries: Mapped[list[PaperSummaryRow]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="PaperSummaryRow.id"
    )
    notes: Mapped[list[Note]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="Note.id"
    )
    reading: Mapped[ReadingState | None] = relationship(
        back_populates="paper", cascade="all, delete-orphan", uselist=False
    )


class PaperScore(Base):
    __tablename__ = "paper_scores"
    __table_args__ = (UniqueConstraint("run_id", "paper_id", name="uq_score_run_paper"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(16), index=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    topic_id: Mapped[int | None] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), nullable=True
    )
    score: Mapped[float] = mapped_column(Float, default=0.0)
    label: Mapped[str] = mapped_column(String(16), default="skip")
    reason: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64), default="")

    paper: Mapped[Paper] = relationship(back_populates="scores")


class PaperSummaryRow(Base):
    __tablename__ = "paper_summaries"
    __table_args__ = (UniqueConstraint("run_id", "paper_id", name="uq_summary_run_paper"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(16), index=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    tldr: Mapped[str] = mapped_column(Text, default="")
    problem: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(Text, default="")
    results: Mapped[str] = mapped_column(Text, default="")
    novelty: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64), default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    paper: Mapped[Paper] = relationship(back_populates="summaries")


class Briefing(Base):
    __tablename__ = "briefings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    run_id: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    markdown: Mapped[str] = mapped_column(Text, default="")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="daily")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cursor: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AICall(Base):
    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    port: Mapped[str] = mapped_column(String(32), default="")
    purpose: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    paper: Mapped[Paper] = relationship(back_populates="notes")


class ReadingState(Base):
    __tablename__ = "reading_state"

    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
    )
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    star: Mapped[bool] = mapped_column(Boolean, default=False)
    marked_skip: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    paper: Mapped[Paper] = relationship(back_populates="reading")


class ProfileWeight(Base):
    """M1 画像权重行（kind=category|term|author）：(kind,key) 唯一。

    存**原始累计值**，衰减在读侧计算（免写放大、可审计）；单事件正向限幅在写侧。
    """
    __tablename__ = "profile_weights"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_profile_kind_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    key: Mapped[str] = mapped_column(String(200), index=True)
    w: Mapped[float] = mapped_column(Float, default=0.0)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PaperTag(Base):
    """一论文一枚图论标签（六色图例）：重跑替换，undo 还原旧标。"""
    __tablename__ = "paper_tags"

    arxiv_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag: Mapped[str] = mapped_column(String(32), default="")
    actor: Mapped[str] = mapped_column(String(32), default="")
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CitationEdge(Base):
    """库内引文边：src 引用 dst（S2 文献）。重跑全量替换，幂等。

    ``direction`` 区分正向（``cites``：本篇引用的）与反查（``cited_by``：引用了本篇的）——
    同一张表两种语义，靠"谁是 src"隐含推断会让"在库出发"与"下游扩散"混为一谈；
    ``year`` 是 S2 已经返回、此前落库被丢掉的字段——年代编排（timeline 布局）靠它。
    """
    __tablename__ = "citation_edges"
    __table_args__ = (UniqueConstraint("src_arxiv_id", "dst_arxiv_id", name="uq_cite_edge"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    src_arxiv_id: Mapped[str] = mapped_column(String(64), index=True)
    dst_arxiv_id: Mapped[str] = mapped_column(String(64), index=True)
    dst_title: Mapped[str] = mapped_column(Text, default="")
    dst_citations: Mapped[int] = mapped_column(Integer, default=0)
    influential: Mapped[bool] = mapped_column(Boolean, default=False)
    direction: Mapped[str] = mapped_column(String(16), default="cites")   # cites|cited_by
    year: Mapped[int] = mapped_column(Integer, default=0)                 # 0=未知
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FeedIssue(Base):
    """feed 一期快照（由 AI 经命令面 publish_feed 发布）：/feed 面板只读最新期，

    **不现场重算**——换页/改口味的唯一入口是对话里使唤 AI。快照只钉“哪些篇、
    什么序、为什么”；中文摘要等富化字段渲染时现 join（摘要更新跟着最新走）。
    """
    __tablename__ = "feed_issues"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)   # limit/days/mix/offset/quotas/seen_days
    items: Mapped[list] = mapped_column(JSON, default=list)    # 装配原序条目
    actor: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(Text, default="")


class GraphView(Base):
    """图视图一期快照（由 AI 经命令面 set_graph_view 发布）：/network 默认渲染"默认视图"。

    **视图是一等公民**：根/深度/布局/分组/着色/标签/预算/锚点全在这一行 spec 里，
    HTML 渲染与 /network.json 回执读的是同一份 spec ⇒ "AI 画的东西"与"页面显示的东西"
    不再是两处真相（这正是此前"只能靠 URL 与 /settings 两个旁路"的病根）。
    """

    __tablename__ = "graph_views"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    actor: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Event(Base):
    """append-only 事件（L2 记录仪，docs/GAPS.md §3）。

    只 INSERT，不 UPDATE/DELETE——由 `infra/db.py` 的触发器在数据库层钉死
    （判据：任何 UPDATE/DELETE 必须失败）。字段对齐 Energy Level mecha/bus.py：
    actor（谁写的）/ reason（为什么）/ op / target / before / after / reversible。
    """

    __tablename__ = "events"

    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(32), default="", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    op: Mapped[str] = mapped_column(String(48), default="", index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reversible: Mapped[int] = mapped_column(Integer, default=1)
