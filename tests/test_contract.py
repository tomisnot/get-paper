"""AI 契约测试：统一框架就绪前，先把契约本身钉死。

- LLMRanker / LLMSummarizer 用 StubLLMPort（固定返回 JSON）验证 prompt→解析→校验链路
- 非法输出必须抛错而不是静默吞掉（DESIGN.md §4.3 第 2 条）
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from paperpilot.domain.models import PaperSummary
from paperpilot.infra.ai import (
    HeuristicRanker,
    HeuristicSummarizer,
    LLMRanker,
    LLMSummarizer,
    UnifiedAIAdapter,
    UnifiedAINotReady,
    unified_available,
)
from paperpilot.infra.ai.llm_based import AIParseError


class StubLLMPort:
    """测试替身：记录调用、返回预制文本。"""

    name = "stub-llm"

    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    def complete(self, *, messages, temperature=0.2, max_tokens=2048, json_mode=False):
        self.calls.append(
            {"messages": messages, "temperature": temperature, "json_mode": json_mode}
        )
        from paperpilot.domain.models import LLMResult

        return LLMResult(text=self.response, model="stub", prompt_tokens=10, completion_tokens=20)


def _profile():
    return SimpleNamespace(
        name="推理",
        description="test-time compute",
        keywords=["reasoning", "chain-of-thought"],
        authors=[],
    )


def _papers(n=3):
    return [
        SimpleNamespace(
            id=i,
            arxiv_id=f"2608.0000{i}",
            title=f"Paper {i} on reasoning",
            abstract=f"We study reasoning with chain-of-thought in paper {i}.",
            authors=[f"Author {i}"],
            primary_category="cs.CL",
        )
        for i in range(1, n + 1)
    ]


def test_llm_ranker_parses_valid_response():
    payload = {
        "scores": [
            {"i": 1, "score": 0.91, "label": "must_read", "reason": "高度相关", "tags": ["reasoning"]},
            {"i": 2, "score": 0.62, "label": "worth", "reason": "相关", "tags": []},
            {"i": 3, "score": 0.31, "label": "skip", "reason": "不太相关", "tags": []},
        ]
    }
    llm = StubLLMPort(json.dumps(payload, ensure_ascii=False))
    ranker = LLMRanker(llm)

    scores = ranker.score_batch(papers=_papers(), profile=_profile(), run_id="t1")

    assert [round(s.score, 2) for s in scores] == [0.91, 0.62, 0.31]
    assert scores[0].label == "must_read"
    assert scores[0].reason == "高度相关"
    assert llm.calls and llm.calls[0]["json_mode"] is True
    assert any(m.role == "system" for m in llm.calls[0]["messages"])


def test_llm_ranker_tolerates_markdown_wrapped_json():
    payload = {"scores": [{"i": 1, "score": 0.8, "label": "worth"}]}
    wrapped = f"```json\n{json.dumps(payload)}\n```"
    ranker = LLMRanker(StubLLMPort(wrapped))
    scores = ranker.score_batch(papers=_papers(1), profile=_profile(), run_id="t")
    assert scores[0].score == 0.8


def test_llm_ranker_rejects_garbage():
    ranker = LLMRanker(StubLLMPort("抱歉，我无法完成该任务。"))
    with pytest.raises(AIParseError):
        ranker.score_batch(papers=_papers(1), profile=_profile(), run_id="t")


def test_llm_ranker_rejects_length_mismatch():
    payload = {"scores": [{"i": 1, "score": 0.8, "label": "worth"}]}
    ranker = LLMRanker(StubLLMPort(json.dumps(payload)))
    with pytest.raises(AIParseError):
        ranker.score_batch(papers=_papers(3), profile=_profile(), run_id="t")


def test_llm_summarizer_parses_valid_response():
    payload = {
        "tldr": "提出自适应停止准则",
        "problem": "思维链过长损害性能",
        "method": "按题目分配计算量",
        "results": "MATH-500 提升 11.4%",
        "novelty": "首个任务相关阈值",
        "keywords": ["reasoning", "test-time compute"],
    }
    summarizer = LLMSummarizer(StubLLMPort(json.dumps(payload, ensure_ascii=False)))
    summary = summarizer.summarize(paper=_papers(1)[0], profile=_profile(), run_id="t")

    assert isinstance(summary, PaperSummary)
    assert summary.tldr == "提出自适应停止准则"
    assert summary.keywords == ["reasoning", "test-time compute"]


def test_heuristic_ports_work_without_any_model():
    profile = _profile()
    papers = _papers(2)
    scores = HeuristicRanker().score_batch(papers=papers, profile=profile, run_id="t")
    assert len(scores) == 2 and all(0.0 <= s.score <= 1.0 for s in scores)
    summary = HeuristicSummarizer().summarize(paper=papers[0], profile=profile, run_id="t")
    assert isinstance(summary, PaperSummary) and summary.tldr


def test_unified_adapter_not_ready_raises():
    # 无 key 时构造即抛（可教学：告诉你设哪个环境变量），而不是留到调用才炸
    assert unified_available() is False
    with pytest.raises(UnifiedAINotReady) as exc_info:
        UnifiedAIAdapter(model="m")
    assert "PAPERPILOT_API_KEY" in str(exc_info.value)
