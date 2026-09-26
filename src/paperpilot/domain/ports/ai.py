"""AI 契约（冻结条款，见 DESIGN.md §4.2 / §4.3）。

分层：
    LLMPort           ← 统一 AI 框架唯一必须实现的契约
    RankerPort        ← 相关性打分；默认实现 LLMRanker 组合自 LLMPort
    SummarizerPort    ← 结构化精读；默认实现 LLMSummarizer 组合自 LLMPort

改契约 = 双向评审：本项目 + AI 框架对接方。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..models import LLMResult, Message, PaperSummary, RelevanceScore

if TYPE_CHECKING:  # 避免领域层运行时依赖 ORM
    from ...infra.orm import Paper, Topic


@runtime_checkable
class LLMPort(Protocol):
    """统一 AI 框架适配器的最小接口（唯一硬契约）。"""

    def complete(
        self,
        *,
        messages: Sequence[Message],
        temperature: float = 0.2,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> LLMResult:
        """发送消息并返回结果。

        要求：
        - json_mode=True 时必须返回合法 JSON 文本（本侧会做 pydantic 二次校验）
        - 失败必须抛错（限流/超时/服务不可用分别抛可区分的异常），不得返回垃圾
        """
        ...


@runtime_checkable
class RankerPort(Protocol):
    """论文相关性打分。实现必须保证返回值长度与 papers 一致。"""

    def score_batch(
        self,
        *,
        papers: Sequence[Paper],
        profile: Topic,
        run_id: str,
    ) -> list[RelevanceScore]:
        ...


@runtime_checkable
class SummarizerPort(Protocol):
    """单篇论文结构化精读。"""

    def summarize(
        self,
        *,
        paper: Paper,
        profile: Topic,
        run_id: str,
    ) -> PaperSummary:
        ...
