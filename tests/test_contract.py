"""确定性基线契约：模型接入由 DSH/MCP 负责。"""

from __future__ import annotations

from types import SimpleNamespace

from paperpilot.domain.models import InterestBrief, PaperSummary
from paperpilot.infra.ai import BaselineRanker, BaselineSummarizer


def _interest() -> InterestBrief:
    return InterestBrief(
        name="推理",
        description="test-time compute",
        keywords=["reasoning"],
        weights={("term", "reasoning"): 0.5, ("category", "cs.CL"): 0.5},
    )


def _paper():
    return SimpleNamespace(
        arxiv_id="2608.00001",
        title="Reasoning paper",
        abstract="We study reasoning with chain-of-thought.",
        authors=["Author"],
        primary_category="cs.CL",
    )


def test_deterministic_baseline_is_available_without_model():
    paper = _paper()
    scores = BaselineRanker().score_batch(
        papers=[paper], interest=_interest(), run_id="t"
    )
    assert len(scores) == 1 and 0.0 <= scores[0].score <= 1.0
    summary = BaselineSummarizer().summarize(
        paper=paper, interest=_interest(), run_id="t"
    )
    assert isinstance(summary, PaperSummary) and summary.tldr
