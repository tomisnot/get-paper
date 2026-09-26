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
