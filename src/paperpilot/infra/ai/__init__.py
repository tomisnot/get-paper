"""项目内保留的确定性基线实现。

内容型 AI 走 DSH/MCP；这里不再提供模型客户端、LLM port 或供应商适配器。
"""

from .baseline import BaselineRanker, BaselineSummarizer

__all__ = [
    "BaselineRanker",
    "BaselineSummarizer",
]
