"""策略层测试：硬规则 / 配额 / 兜底打分 / 抽取式摘要。"""

from __future__ import annotations

from types import SimpleNamespace

from paperpilot.domain.policy import (
    RuleGate,
    SelectionPolicy,
    extractive_summary,
    fallback_keyword_score,
    select_for_briefing,
)


def _paper(arxiv_id, *, title="", abstract="", authors=(), primary="cs.CL"):
    return SimpleNamespace(
        id=abs(hash(arxiv_id)) % 100000,
        arxiv_id=arxiv_id,
        title=title,
        abstract=abstract,
        authors=list(authors),
        primary_category=primary,
        published_at=None,
    )


def _topic(**kw):
    base = dict(
        name="t",
        description="",
        keywords=["reasoning", "chain-of-thought"],
        exclude_keywords=["survey"],
        categories=["cs.CL"],
        authors=[],
        quota=2,
        threshold=0.5,
        enabled=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_rule_gate_category_whitelist():
    gate = RuleGate()
    paper = _paper("1", primary="cs.LG")
    kept, rejected = gate.apply([paper], _topic())
    assert kept == [] and rejected == [paper]


def test_rule_gate_exclude_keyword():
    gate = RuleGate()
    paper = _paper("1", title="A survey of reasoning", abstract="reasoning methods")
    kept, rejected = gate.apply([paper], _topic())
    assert rejected == [paper] and kept == []


def test_rule_gate_rejects_non_english_title():
    gate = RuleGate(require_english=True)
    paper = _paper("1", title="关于推理模型的研究")
    kept, rejected = gate.apply([paper], _topic())
    assert rejected == [paper]


def test_fallback_score_ranks_relevant_higher():
    topic = _topic(keywords=["reasoning", "chain-of-thought"])
    relevant = _paper("1", title="Reasoning with chain-of-thought", abstract="We study reasoning.")
    irrelevant = _paper("2", title="Graph neural networks", abstract="Traffic forecasting.")
    s1 = fallback_keyword_score(relevant, topic)
    s2 = fallback_keyword_score(irrelevant, topic)
    assert s1.score > s2.score
    assert s1.label in ("must_read", "worth")
    assert "reasoning" in s1.tags


def test_select_respects_author_cap_and_quota():
    policy = SelectionPolicy(threshold=0.5, quota_per_topic=2, max_papers=10,
                             max_per_author=1, must_read_cap=3)
    items = [
        (_paper("a", authors=["X"]), _score(0.9, "worth")),
        (_paper("b", authors=["X"]), _score(0.85, "worth")),   # 同作者 → 存档
        (_paper("c", authors=["Y"]), _score(0.7, "worth")),
        (_paper("d", authors=["Z"]), _score(0.6, "worth")),    # 超出 quota → 存档
        (_paper("e", authors=["W"]), _score(0.2, "worth")),    # 低于阈值 → 存档
    ]
    selected, archived = select_for_briefing(items, policy)
    ids = [p.arxiv_id for p, _ in selected]
    assert ids == ["a", "c"]
    assert {p.arxiv_id for p, _ in archived} == {"b", "d", "e"}


def test_select_must_read_cap():
    policy = SelectionPolicy(threshold=0.5, quota_per_topic=2, max_papers=10,
                             max_per_author=5, must_read_cap=1)
    items = [
        (_paper("a", authors=["AA"]), _score(0.9, "must_read")),
        (_paper("b", authors=["BB"]), _score(0.88, "must_read")),  # 超出 must_read_cap
        (_paper("c", authors=["CC"]), _score(0.7, "worth")),
    ]
    selected, archived = select_for_briefing(items, policy)
    assert [p.arxiv_id for p, _ in selected] == ["a", "c"]
    assert {p.arxiv_id for p, _ in archived} == {"b"}


def test_extractive_summary_extracts_without_hallucinating():
    abstract = (
        "We address the problem of noisy rewards. "
        "However, existing estimators remain sensitive to label noise. "
        "We propose a robust estimator that filters label noise. "
        "Experiments show that accuracy improves by 12% on three benchmarks."
    )
    paper = _paper("1", abstract=abstract)
    summary = extractive_summary(paper)
    assert summary.tldr == "We address the problem of noisy rewards."  # 首句
    assert summary.problem == "However, existing estimators remain sensitive to label noise."
    assert summary.method == "We propose a robust estimator that filters label noise."
    assert summary.results == "Experiments show that accuracy improves by 12% on three benchmarks."
    assert summary.novelty == ""  # 没有 novelty 线索词就留空，不编造
    assert summary.keywords
    # 各字段不得重复同一句
    fields = [summary.problem, summary.method, summary.results]
    assert len(set(fields)) == len([f for f in fields if f])


def _score(score, label):
    from paperpilot.domain.models import RelevanceScore

    return RelevanceScore(score=score, label=label, reason="", tags=[])
