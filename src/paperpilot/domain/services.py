"""检索与阅读态门面：Web 层只跟这个服务打交道。

actor 约定（GAPS.md §2）：Web GUI 的写入归人所有 → 默认 actor="human"。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .ports.repo import PaperRepository


class RetrievalService:
    def __init__(self, repo: PaperRepository) -> None:
        self.repo = repo

    # ---- 简报 ----
    def today_briefing(self):
        return self.repo.briefing_for_date(date.today().isoformat())

    def briefing(self, date_str: str):
        return self.repo.briefing_for_date(date_str)

    def recent_briefings(self, limit: int = 30):
        return self.repo.briefings(limit=limit)

    # ---- 论文 ----
    def detail(self, arxiv_id: str) -> dict[str, Any] | None:
        paper = self.repo.get_paper(arxiv_id)
        if paper is None:
            return None
        return {
            "paper": paper,
            "scores": self.repo.latest_scores(paper),
            "summary": self.repo.latest_summary(paper),
            "notes": self.repo.notes_for(paper),
            "reading": paper.reading,
        }

    def search(
        self,
        query: str = "",
        *,
        label: str | None = None,
        primary_category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        return self.repo.search_papers(
            query, label=label, primary_category=primary_category, limit=limit, offset=offset
        )

    # ---- 人工状态（actor 默认 human：Web 是人在用）----
    def _pref(self, arxiv_id: str, signal: str, on: bool) -> None:
        """Web 入口表达的偏好也要喂画像（与 AI 侧**同表同权**）。

        只记打开方向：信号表没有"取消收藏/标未读"这两档，硬造负值会把"取消"误当"讨厌"。
        """
        if not on:
            return
        try:
            self.repo.record_signal(arxiv_id, signal, actor="human",
                                    reason=f"Web 面板表达偏好：{signal}")
        except Exception:  # noqa: BLE001  记账失败不拦页面
            import logging
            logging.getLogger("paperpilot.services").exception("偏好信号记账失败（操作照常）")

    def toggle_read(self, arxiv_id: str) -> bool | None:
        paper = self.repo.get_paper(arxiv_id)
        if paper is None:
            return None
        current = bool(paper.reading.read) if paper.reading else False
        self.repo.set_read(paper, read=not current, actor="human", reason="Web 面板切换已读")
        self._pref(arxiv_id, "read", not current)
        return True

    def star(self, arxiv_id: str) -> bool | None:
        paper = self.repo.get_paper(arxiv_id)
        if paper is None:
            return None
        star = self.repo.toggle_star(paper, actor="human", reason="Web 面板收藏")
        self._pref(arxiv_id, "star", bool(star))
        return star

    def skip(self, arxiv_id: str) -> bool | None:
        paper = self.repo.get_paper(arxiv_id)
        if paper is None:
            return None
        self.repo.set_marked_skip(paper, skip=True, actor="human", reason="Web 面板标不感兴趣")
        self._pref(arxiv_id, "uninterested", True)      # 负反馈：与 AI 的 skip_paper 同权
        return True

    def add_note(self, arxiv_id: str, content: str) -> bool:
        paper = self.repo.get_paper(arxiv_id)
        if paper is None or not content.strip():
            return False
        self.repo.add_note(paper, content.strip(), actor="human", reason="Web 面板加笔记")
        return True

    def delete_note(self, note_id: int, *, actor: str = "human",
                    reason: str = "Web 面板删笔记") -> None:
        """删笔记。⚠ 这是**人类独有的管理动作**（不与 AI 争写）⇒ 命令面有它、**AI 工具面没有它**；
        但归因/理由要能由调用方给（命令面把**通道的 actor** 传进来，别再写死）。"""
        self.repo.delete_note(note_id, actor=actor, reason=reason)
