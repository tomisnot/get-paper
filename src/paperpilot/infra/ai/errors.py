"""AI 可教学错误体系（移植自 Energy Level `src/mecha/errors.py` 的模式）。

设计依据（Energy Level docs/AI交互-架构设计.md，纲领 D2）：**错误消息是 LLM 的 UI**。
一条能自己教调用方改对的错误，比十句散文都有用。因此所有 AI 侧错误都携带：
  kind    机器可读类别（rate_limit / auth / balance / context_length / parse / ...）
  hint    一句"下一步该怎么办"
  suggest 候选纠正（如相似的参数名），宁缺毋滥

同时服务两个消费者：流水线降级提示（人看）与 ai_calls 记账（机器看）。
"""

from __future__ import annotations

__all__ = [
    "AIError",
    "AIParseError",
    "LLMHTTPError",
    "LLMTimeoutError",
    "UnifiedAINotReady",
]


class AIError(RuntimeError):
    """AI 层结构化错误基类。"""

    kind = "ai_error"

    def __init__(
        self,
        message: str,
        *,
        kind: str | None = None,
        hint: str | None = None,
        suggest=None,
        **extra,
    ) -> None:
        super().__init__(message)
        if kind:
            self.kind = kind
        self.message = message
        self.hint = hint
        self.suggest = list(suggest or [])
        self.extra = extra

    def to_dict(self) -> dict:
        d: dict = {"kind": self.kind, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        if self.suggest:
            d["suggest"] = self.suggest
        if self.extra:
            d.update(self.extra)
        return d

    def __str__(self) -> str:
        parts = [self.message]
        if self.suggest:
            parts.append("候选：" + "、".join(self.suggest))
        if self.hint:
            parts.append(self.hint)
        return "  ".join(parts)


class AIParseError(AIError):
    """模型输出无法解析为合法 JSON / 通过 schema 校验。"""

    kind = "parse"


class LLMHTTPError(AIError):
    """LLM 接口返回非 2xx。kind 由状态码映射（见 unified.py 的 _STATUS_KINDS）。"""

    kind = "http_error"

    def __init__(self, message: str, *, status: int, kind: str, hint: str | None = None, **extra) -> None:
        super().__init__(message, hint=hint, status=status, **extra)
        self.kind = kind
        self.status = status


class LLMTimeoutError(AIError):
    kind = "timeout"


class UnifiedAINotReady(AIError):
    """未配置 API key，unified 档不可用。"""

    kind = "not_ready"
