"""仓储契约：DailyPipelineService / RetrievalService 需要的最小数据面。

定义在领域层，使业务逻辑不依赖 infra 实现，也便于测试替身。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from ...infra.arxiv import NormalizedPaper
    from ...infra.orm import AICall, Briefing, Note, Paper, PaperScore, PaperSummaryRow, Run, Topic
    from ..models import PaperSummary, RelevanceScore


class PaperRepository(Protocol):
    # ---- events（L2 记录仪，append-only；GAPS.md §3）----
    def events_since(
        self,
        *,
        since_seq: int = 0,
        actor: str = "",
        op: str = "",
        limit: int = 50,
    ) -> dict: ...

    def undo(self, seq: int = 0, *, actor: str, reason: str = "") -> dict: ...

    # ---- topics ----
    def sync_topics(
        self, topics: Sequence, *, actor: str = "system", reason: str = ""
    ) -> list[Topic]: ...

    def enabled_topics(self) -> list[Topic]: ...

    # ---- papers ----
    def upsert_papers(
        self, items: Iterable[NormalizedPaper], *, actor: str = "system", reason: str = ""
    ) -> dict[str, int]: ...

    def candidates_for_topic(self, topic: Topic, *, lookback_days: int) -> list[Paper]: ...

    def mark_status(
        self, paper: Paper, status: str, *, actor: str = "system", reason: str = ""
    ) -> None: ...

    def reset_statuses(
        self,
        arxiv_ids: Sequence[str],
        status: str = "new",
        *,
        actor: str = "system",
        reason: str = "",
    ) -> int: ...

    def get_paper(self, arxiv_id: str) -> Paper | None: ...

    def recent_papers(self, *, limit: int = 50, status: str | None = None) -> list[Paper]: ...

    # ---- scores / summaries ----
    def save_scores(
        self,
        *,
        run_id: str,
        paper: Paper,
        topic_id: int | None,
        score: RelevanceScore,
        model: str,
    ) -> None: ...

    def latest_scores(self, paper: Paper) -> list[PaperScore]: ...

    def save_summary(
        self,
        *,
        run_id: str,
        paper: Paper,
        summary: PaperSummary,
        model: str,
        tokens: int = 0,
        latency_ms: int = 0,
    ) -> None: ...

    def latest_summary(self, paper: Paper) -> PaperSummaryRow | None: ...

    # ---- briefings / runs ----
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
    ) -> Briefing: ...

    def briefing_for_date(self, date: str) -> Briefing | None: ...

    def briefings(self, *, limit: int = 30) -> list[Briefing]: ...

    def create_run(self, run_id: str, *, mode: str = "daily") -> Run: ...

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        stats: dict | None = None,
        error: str | None = None,
    ) -> None: ...

    def last_run(self) -> Run | None: ...

    # ---- ai 记账 ----
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
    ) -> None: ...

    def ai_calls_since(self, since: datetime, *, limit: int = 100) -> list[AICall]: ...

    # ---- 检索 ----
    def search_papers(
        self,
        query: str,
        *,
        label: str | None = None,
        primary_category: str | None = None,
        limit: int = 50,
    ) -> list[Paper]: ...

    # ---- 人工状态 ----
    def set_read(
        self, paper: Paper, *, read: bool, actor: str = "system", reason: str = ""
    ) -> None: ...

    def toggle_star(self, paper: Paper, *, actor: str = "system", reason: str = "") -> bool: ...

    def set_marked_skip(
        self, paper: Paper, *, skip: bool, actor: str = "system", reason: str = ""
    ) -> None: ...

    def add_note(
        self, paper: Paper, content: str, *, actor: str = "system", reason: str = ""
    ) -> Note: ...

    def delete_note(
        self, note_id: int, *, actor: str = "system", reason: str = ""
    ) -> None: ...

    def notes_for(self, paper: Paper) -> list[Note]: ...

    def backup_ok(self) -> None: ...
