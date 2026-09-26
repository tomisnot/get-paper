"""AI 实现三档（DESIGN.md §4.4）：

- heuristic  ：本地 Mock（关键词重合度打分 + 抽取式摘要），不需要任何模型
- llm_based  ：跑在 LLMPort 上的默认实现，等统一框架就绪即可用
- unified    ：统一 AI 框架适配器（外部依赖，未就绪时抛错并自动降级）
"""

from .heuristic import HeuristicRanker, HeuristicSummarizer
from .llm_based import LLMRanker, LLMSummarizer
from .unified import UnifiedAIAdapter, UnifiedAINotReady, unified_available

__all__ = [
    "HeuristicRanker",
    "HeuristicSummarizer",
    "LLMRanker",
    "LLMSummarizer",
    "UnifiedAIAdapter",
    "UnifiedAINotReady",
    "unified_available",
]
