"""跑在 LLMPort 上的默认 AI 实现。

结构（DESIGN.md §4.1 / §17）：
    统一 AI 框架 / OpenAI 兼容接口 → UnifiedAIAdapter(LLMPort) → LLMRanker / LLMSummarizer

这里把 prompt 模板、JSON 解析、pydantic 校验、错误可教学化全部先写好并测好，
未来只剩「换 LLMPort 实现」这一件事。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from ...domain.models import (
    LLMResult,
    Message,
    PaperSummary,
    RelevanceScore,
)
from ...domain.ports.ai import LLMPort
from .errors import AIError

logger = logging.getLogger("paperpilot.ai")

_RANK_SYSTEM = """你是一个严格的科研论文相关性评估器。
给定用户的研究兴趣画像和一批论文（标题+摘要），判断每篇论文与用户兴趣的相关程度。
评分标准：
- 0.9-1.0 must_read：直接命中用户核心关注点，方法或结论有新意
- 0.6-0.8 worth：与兴趣明显相关，值得一看
- 0.0-0.5 skip：不相关或只边缘相关
只依据给定的标题和摘要判断，不得使用论文之外的先验知识。
必须严格输出 JSON，不要输出任何其他文字。"""

_RANK_USER = """研究兴趣画像：
- 名称：{topic}
- 描述：{description}
- 关键词：{keywords}

论文列表（编号从 1 开始）：
{papers}

请输出 JSON：
{{"scores": [{{"i": 1, "score": 0.91, "label": "must_read", "reason": "一句话中文理由（<=60字）", "tags": ["关键词1", "关键词2"]}}, ...]}}
scores 数组长度必须等于论文数量，i 为论文编号。"""

_SUM_SYSTEM = """你是一个科研论文精读助手。
只依据给定的标题和摘要进行总结，不得补充论文中不存在的信息，不得编造数字。
全部字段用简体中文。严格输出 JSON，不要输出任何其他文字。"""

_SUM_USER = """研究兴趣画像：{topic}（{description}）

论文标题：{title}
论文摘要：
{abstract}

请输出 JSON：
{{"tldr": "一句话结论（<=100字）", "problem": "解决什么问题（<=80字）", "method": "怎么做的（<=120字）", "results": "关键结果与数字（<=120字）", "novelty": "主要贡献点（<=80字）", "keywords": ["关键词1", "关键词2", "关键词3"]}}"""


class AIParseError(AIError):
    """AI 输出无法解析为合法 JSON / 通过 schema 校验（可教学：kind=parse）。"""

    kind = "parse"


def _extract_json(text: str):
    """从模型输出中稳健提取 JSON 对象（容忍 ```json 包裹与前后缀文字）。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = [line for line in raw.splitlines() if not line.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AIParseError(
            f"输出中未找到 JSON 对象: {raw[:120]!r}",
            hint="在系统提示中已要求只输出 JSON；若仍失败，"
            "检查 ai.base_url 指向的模型是否遵循 json_mode",
        )
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AIParseError(
            f"JSON 解析失败: {exc}; 原文: {raw[:120]!r}",
            hint="多为 JSON 被 max_tokens 截断：调大 max_tokens 或减小批量",
        ) from exc


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


class LLMRanker:
    """用 LLMPort 做相关性批量打分（一次 prompt 评一批，控成本）。"""

    name = "llm-ranker"

    def __init__(self, llm: LLMPort, *, max_abstract_chars: int = 1600, batch_size: int = 8):
        self.llm = llm
        self.max_abstract_chars = max_abstract_chars
        self.batch_size = batch_size

    def score_batch(self, *, papers, interest, run_id: str) -> list[RelevanceScore]:
        results: list[RelevanceScore] = []
        for start in range(0, len(papers), self.batch_size):
            batch = list(papers[start : start + self.batch_size])
            results.extend(self._score_batch_once(batch, interest))
        return results

    def _score_batch_once(self, batch, interest) -> list[RelevanceScore]:
        papers_text = "\n\n".join(
            f"[{i}] 标题：{_truncate(p.title, 300)}\n摘要：{_truncate(p.abstract, self.max_abstract_chars)}"
            for i, p in enumerate(batch, start=1)
        )
        messages = [
            Message(role="system", content=_RANK_SYSTEM),
            Message(
                role="user",
                content=_RANK_USER.format(
                    topic=interest.name,
                    description=interest.description or "",
                    keywords=", ".join(interest.keywords or []),
                    papers=papers_text,
                ),
            ),
        ]
        payload = self._complete_json(messages)
        raw_scores = payload.get("scores")
        if not isinstance(raw_scores, list) or len(raw_scores) != len(batch):
            raise AIParseError(f" scores 长度不符: {len(batch)} vs {len(raw_scores or [])}")

        by_index = {int(item.get("i", 0)): item for item in raw_scores}
        out: list[RelevanceScore] = []
        for i in range(1, len(batch) + 1):
            item = by_index.get(i)
            if item is None:
                raise AIParseError(f"缺少编号 {i} 的评分")
            out.append(
                RelevanceScore(
                    score=float(item["score"]),
                    label=str(item.get("label", "skip")),
                    reason=str(item.get("reason", "")),
                    tags=[str(t) for t in item.get("tags", [])][:5],
                )
            )
        return out

    def _complete_json(self, messages: Sequence[Message]):
        result: LLMResult = self.llm.complete(
            messages=messages, temperature=0.1, max_tokens=2048, json_mode=True
        )
        return _extract_json(result.text)


class LLMSummarizer:
    """用 LLMPort 做单篇结构化精读。"""

    name = "llm-summarizer"

    def __init__(self, llm: LLMPort, *, max_abstract_chars: int = 2000):
        self.llm = llm
        self.max_abstract_chars = max_abstract_chars

    def summarize(self, *, paper, interest, run_id: str) -> PaperSummary:
        messages = [
            Message(role="system", content=_SUM_SYSTEM),
            Message(
                role="user",
                content=_SUM_USER.format(
                    topic=interest.name,
                    description=interest.description or "",
                    title=_truncate(paper.title, 400),
                    abstract=_truncate(paper.abstract, self.max_abstract_chars),
                ),
            ),
        ]
        payload = self._complete_json(messages)
        return PaperSummary(
            tldr=str(payload.get("tldr", "")),
            problem=str(payload.get("problem", "")),
            method=str(payload.get("method", "")),
            results=str(payload.get("results", "")),
            novelty=str(payload.get("novelty", "")),
            keywords=[str(k) for k in payload.get("keywords", [])][:8],
        )

    def _complete_json(self, messages: Sequence[Message]):
        result: LLMResult = self.llm.complete(
            messages=messages, temperature=0.2, max_tokens=1024, json_mode=True
        )
        return _extract_json(result.text)
