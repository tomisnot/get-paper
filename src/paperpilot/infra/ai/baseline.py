"""确定性基线：画像池打分与抽取式摘要，不调用模型。"""

from __future__ import annotations

from ...domain.models import PaperSummary, RelevanceScore
from ...domain.policy import extractive_summary, fallback_pool_score


class BaselineRanker:
    name = "baseline-ranker"

    def score_batch(self, *, papers, interest, run_id: str) -> list[RelevanceScore]:
        return [fallback_pool_score(p, interest.weights) for p in papers]


class BaselineSummarizer:
    name = "baseline-summarizer"

    def summarize(self, *, paper, interest, run_id: str) -> PaperSummary:
        return extractive_summary(paper)
