"""Heuristic AI：本地确定性 Mock，保证 AI 框架缺席时全链路可开发、可测试、可演示。

打分/摘要逻辑本体在 domain/policy.py（纯函数），这里只做薄封装实现 Port。
"""

from __future__ import annotations

from ...domain.models import PaperSummary, RelevanceScore
from ...domain.policy import extractive_summary, fallback_keyword_score


class HeuristicRanker:
    """关键词重合度伪相关性打分。"""

    name = "heuristic-ranker"

    def score_batch(self, *, papers, profile, run_id: str) -> list[RelevanceScore]:
        return [fallback_keyword_score(p, profile) for p in papers]


class HeuristicSummarizer:
    """抽取式摘要：从 abstract 按线索词抽原句，不编造。"""

    name = "heuristic-summarizer"

    def summarize(self, *, paper, profile, run_id: str) -> PaperSummary:
        return extractive_summary(paper)
