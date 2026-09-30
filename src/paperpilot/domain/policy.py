"""纯函数策略层：硬规则过滤、关键词兜底打分、简报配额、抽取式摘要。

全部不依赖 IO，可单测；AI 缺席或失败时的降级逻辑也收敛在这里。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .models import PaperSummary, RelevanceScore

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "have",
    "has", "been", "our", "their", "its", "which", "when", "using", "based", "such",
    "these", "those", "into", "over", "under", "between", "than", "then", "also",
    "can", "could", "may", "might", "will", "would", "should", "does", "did", "not",
    "but", "all", "any", "each", "more", "most", "other", "some", "only", "very",
    "paper", "propose", "proposed", "show", "shows", "results", "result", "method",
    "methods", "approach", "model", "models", "task", "tasks", "data", "learning",
    "performance", "large", "language",
}

_PROBLEM_CUES = ("we address", "challenge", "problem", "limitation", "bottleneck", "however", "remains", "suffers")
_METHOD_CUES = ("we propose", "we present", "we introduce", "we develop", "our approach", "framework", "architecture", "we design", "we train")
_RESULT_CUES = ("results show", "experiments", "achieve", "achieves", "improve", "improves", "outperform", "state-of-the-art", "%", "benchmark")
_NOVELTY_CUES = ("first", "novel", "new", "unlike", "contrary")


# ---------------------------------------------------------------- 工具
def _paper_text(paper) -> str:
    return f"{paper.title or ''}\n{paper.abstract or ''}"


def matched_keywords(paper, topic) -> list[str]:
    """返回在标题/摘要中命中的主题关键词（小写子串匹配，多词短语原样）。"""
    text = _paper_text(paper).lower()
    return [kw for kw in (topic.keywords or []) if kw.lower() in text]


def matched_authors(paper, topic) -> list[str]:
    want = {a.strip().lower() for a in (topic.authors or []) if a.strip()}
    if not want:
        return []
    have = {a.strip().lower() for a in (paper.authors or []) if a and a.strip()}
    return sorted(want & have)


# ---------------------------------------------------------------- 兜底打分（Mock 与降级共用）
def fallback_pool_score(paper, weights) -> RelevanceScore:
    """**画像池兜底打分**（日报线的基线分 & AI 缺席时的降级）。

    与推荐流用**同一个** `profile.score_paper` ⇒ 两条线一个真相。原始分经
    `pool_relevance` 压到 0~1，`why` 原样带进 reason（**拿不出 why 的条目不许进简报**）。
    """
    from .profile import label_for, paper_features, pool_relevance, score_paper

    feat = paper_features(list(getattr(paper, "categories", None) or []),
                          getattr(paper, "primary_category", "") or "",
                          getattr(paper, "title", "") or "",
                          getattr(paper, "abstract", "") or "",
                          list(getattr(paper, "authors", None) or []))
    raw, why = score_paper(feat, weights or {})
    rel = pool_relevance(raw)
    return RelevanceScore(
        score=rel, label=label_for(rel),
        reason=("；".join(why[:4]) if why else "画像池无命中：按探索位保留待 AI 判断"),
        tags=[t for t in feat["terms"][:6]],
    )


def fallback_keyword_score(paper, topic) -> RelevanceScore:
    """关键词重合度兜底打分：AI 不可用时保证流水线仍可运行（DESIGN.md §4.4）。"""
    total = len(topic.keywords or [])
    hits = matched_keywords(paper, topic)
    title_hits = [k for k in hits if k.lower() in (paper.title or "").lower()]
    author_hits = matched_authors(paper, topic)

    if not hits:
        score = 0.12
    else:
        coverage = len(hits) / max(total, 1)
        title_ratio = len(title_hits) / max(len(hits), 1)
        score = 0.35 + 0.45 * coverage + 0.15 * title_ratio
    if author_hits:
        score = min(1.0, score + 0.15)
    score = round(min(score, 1.0), 3)

    if score >= 0.78:
        label = "must_read"
    elif score >= 0.5:
        label = "worth"
    else:
        label = "skip"
    reason = f"命中 {len(hits)}/{max(total, 1)} 个关键词: {', '.join(hits)}"
    if title_hits:
        reason += f"；标题命中: {', '.join(title_hits)}"
    if author_hits:
        reason += f"；关注作者: {', '.join(author_hits)}"
    return RelevanceScore(score=score, label=label, reason=reason, tags=hits)


# ---------------------------------------------------------------- 硬规则
@dataclass(frozen=True)
class PoolScope:
    """**全局硬门口径**（主题池化后，门不再挂在每个主题上）。

    主题的"分类白名单/排除词"改走画像（正/负权重，软影响）；这里只留**全局**的硬底线：
    分类白名单（默认取 `settings.arxiv_categories`）、全局排除词、关注/屏蔽作者。
    与 `TopicCfg` 鸭式同形（都有 categories/exclude_keywords），所以 `RuleGate.apply` 两者通吃。
    """

    categories: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateStats:
    category_blocked: int = 0
    keyword_blocked: int = 0
    language_blocked: int = 0
    author_blocked: int = 0


class RuleGate:
    """零成本先砍一刀：分类白名单 / 排除词 / 语言 / 作者黑名单。"""

    def __init__(self, blocked_authors: Sequence[str] = (), require_english: bool = True):
        self.blocked_authors = {a.strip().lower() for a in blocked_authors if a.strip()}
        self.require_english = require_english

    def is_english(self, title: str) -> bool:
        if not self.require_english:
            return True
        return len(_CJK_RE.findall(title or "")) / max(len(title or " "), 1) < 0.2

    def apply(self, papers: Sequence, topic) -> tuple[list, list]:
        kept: list = []
        rejected: list = []
        topic_categories = {c.strip() for c in (topic.categories or []) if c.strip()}
        for paper in papers:
            reason = self._reject_reason(paper, topic, topic_categories)
            if reason:
                rejected.append(paper)
            else:
                kept.append(paper)
        return kept, rejected

    def _reject_reason(self, paper, topic, topic_categories) -> str | None:
        if topic_categories and (paper.primary_category or "") not in topic_categories:
            return f"分类 {paper.primary_category} 不在主题白名单"
        text = _paper_text(paper).lower()
        for kw in topic.exclude_keywords or []:
            if kw and kw.lower() in text:
                return f"命中排除词: {kw}"
        if not self.is_english(paper.title):
            return "疑似非英文标题"
        if self.blocked_authors:
            authors = {a.strip().lower() for a in (paper.authors or []) if a and a.strip()}
            hit = authors & self.blocked_authors
            if hit:
                return f"作者黑名单: {', '.join(sorted(hit))}"
        return None


# ---------------------------------------------------------------- 简报配额
@dataclass(frozen=True)
class SelectionPolicy:
    threshold: float = 0.6
    quota_per_topic: int = 4
    max_papers: int = 12
    max_per_author: int = 1
    must_read_cap: int = 3


def for_topic(base: SelectionPolicy, topic) -> SelectionPolicy:
    """主题级覆盖：threshold 与 quota。"""
    return replace(
        base,
        threshold=topic.threshold if topic.threshold else base.threshold,
        quota_per_topic=topic.quota if topic.quota else base.quota_per_topic,
    )


def _published_ts(paper) -> float:
    ts = paper.published_at
    return ts.timestamp() if ts else 0.0


def select_for_briefing(
    items: Sequence[tuple], policy: SelectionPolicy
) -> tuple[list[tuple], list[tuple]]:
    """items: [(paper, score)] -> (selected, archived)。

    规则（DESIGN.md §6.2）：
    1. 低于阈值 → 存档
    2. must_read 直通，但受 must_read_cap 限制
    3. 每主题 quota 篇（must_read 不占配额）
    4. 同一作者每天最多 max_per_author 篇
    """
    ordered = sorted(items, key=lambda ps: (-ps[1].score, -_published_ts(ps[0])))
    selected: list[tuple] = []
    archived: list[tuple] = []
    author_count: dict[str, int] = {}
    must_read_count = 0

    for paper, score in ordered:
        authors = {a.strip().lower() for a in (paper.authors or []) if a and a.strip()}
        if score.score < policy.threshold:
            archived.append((paper, score))
            continue
        if score.label == "must_read" and must_read_count >= policy.must_read_cap:
            archived.append((paper, score))
            continue
        if score.label != "must_read" and len(selected) >= policy.quota_per_topic:
            archived.append((paper, score))
            continue
        if any(author_count.get(a, 0) >= policy.max_per_author for a in authors):
            archived.append((paper, score))
            continue
        # 入选
        if score.label == "must_read":
            must_read_count += 1
        for a in authors:
            author_count[a] = author_count.get(a, 0) + 1
        selected.append((paper, score))

    return selected, archived


# ---------------------------------------------------------------- 抽取式摘要（AI 缺席兜底）
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def extractive_summary(paper) -> PaperSummary:
    """无 AI 时的兜底总结：按线索词抽取原句，不编造、不重复使用同一句。"""
    sentences = split_sentences(paper.abstract or "")
    text = (paper.abstract or "").lower()
    tokens = re.findall(r"[a-zA-Z][a-zA-Z\-]{3,}", text)
    freq: dict[str, int] = {}
    for tok in tokens:
        tok = tok.lower().strip("-")
        if tok in _STOPWORDS:
            continue
        freq[tok] = freq.get(tok, 0) + 1
    keywords = sorted(freq, key=lambda k: (-freq[k], k))[:5]

    used: set[int] = set()

    def pick(cues: Sequence[str]) -> str:
        for idx, sentence in enumerate(sentences):
            if idx in used:
                continue
            low = sentence.lower()
            if any(cue in low for cue in cues):
                used.add(idx)
                return sentence
        return ""

    # TL;DR 用首句；其余字段按线索词依次抽取，不与已用句子重复
    if sentences:
        used.add(0)
    if sentences and len(sentences[0]) > 120:
        tldr = sentences[0][:117] + "…"
    else:
        tldr = sentences[0] if sentences else ""
    return PaperSummary(
        tldr=tldr,
        problem=pick(_PROBLEM_CUES),
        method=pick(_METHOD_CUES),
        results=pick(_RESULT_CUES),
        novelty=pick(_NOVELTY_CUES),
        keywords=keywords,
    )
