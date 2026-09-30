"""领域 DTO：只服务于「AI 边界」与「简报组装」，不承担持久化。

持久化实体见 infra/orm.py（务实简化：实体即 SQLAlchemy 模型，见 DESIGN.md §11 备注）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

RelevanceLabel = str  # "must_read" | "worth" | "skip"，宽松类型便于 Mock/适配器实现


# ---------------------------------------------------------------- AI 基础契约
class Message(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMResult(BaseModel):
    """统一 AI 框架 complete() 的返回。"""

    text: str
    model: str = "unknown"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


# ---------------------------------------------------------------- 上层能力 DTO
class RelevanceScore(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    label: str
    reason: str = ""
    tags: list[str] = Field(default_factory=list)


class InterestBrief(BaseModel):
    """打分/精读时的「**兴趣上下文**」——来自**画像池**，不再是单个主题。

    为什么要有它（2026-09-30，主题池化）：日报线原来把 `Topic` 对象当"画像"传给打分器，
    于是"兴趣"就等于"一个主题的关键词表"；现在主题只是池子里的种子，**真正的兴趣是池子**。
    * ``weights``：(kind,key)→权重 ⇒ 兜底打分器与推荐流用**同一套**权重（一个真相）；
    * ``name`` / ``description`` / ``keywords``：给 AI prompt 读的人话上下文（保持可解释）。
    """

    name: str = "我的兴趣画像"
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    weights: dict[tuple[str, str], float] = Field(default_factory=dict)


class PaperSummary(BaseModel):
    tldr: str = ""
    problem: str = ""
    method: str = ""
    results: str = ""
    novelty: str = ""
    keywords: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 简报组装
class BriefingItem(BaseModel):
    arxiv_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    primary_category: str = ""
    pdf_url: str = ""
    abs_url: str = ""
    published_at: datetime | None = None
    # 打分
    score: float = 0.0
    label: str = "worth"
    reason: str = ""
    tags: list[str] = Field(default_factory=list)
    # 总结（AI 失败时为抽取式兜底）
    summary: PaperSummary | None = None
    ai_summary: bool = True


class BriefingItemLite(BaseModel):
    arxiv_id: str
    title: str
    primary_category: str = ""
    score: float = 0.0
    label: str = "worth"
    reason: str = ""


class BriefingStats(BaseModel):
    fetched: int = 0
    after_rules: int = 0
    selected: int = 0
    ai_enabled: bool = True
    ai_provider: str = "heuristic"
    ai_latency_ms: int = 0
    degraded: list[str] = Field(default_factory=list)


class BriefingContent(BaseModel):
    date: str
    stats: BriefingStats
    selected: list[BriefingItem] = Field(default_factory=list)
    archived: list[BriefingItemLite] = Field(default_factory=list)
