"""统一 AI 接入适配器（M4 已交付）。

实现选择（DESIGN.md §4.3 / §17）：外部「统一框架」经调研确认是 **MCP 服务端框架**
（Energy Level `src/mecha`，把 GUI 语义面暴露给外部 AI harness），并不提供
"程序主动调 LLM"的客户端。因此本适配器按 **OpenAI 兼容 chat/completions 协议**
直接实现 LLMPort——DeepSeek / 通义 / Kimi / GLM / Ollama / vLLM 等皆兼容，
换后端只改 `ai.base_url` + `ai.model` 两个配置项。

对齐 DeepSeek 官方文档（2026-09）：
- base_url 默认 https://api.deepseek.com，json_mode → response_format=json_object
- 错误码分类：400 格式 / 401 认证 / 402 余额 / 422 参数 / 429 限速 / 500·503 服务端
- JSON Output 偶发空 content → 归为 parse 类错误并给可教学 hint
- 429/5xx/超时指数退避重试；401/402/422/400 立即抛（重试无意义）
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence

import httpx

from ...domain.models import LLMResult, Message
from .errors import AIParseError, LLMHTTPError, LLMTimeoutError, UnifiedAINotReady

logger = logging.getLogger("paperpilot.ai.unified")

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
# API key 环境变量候选（按优先级）；也可在 settings.ai.api_key 直接写
API_KEY_ENVS = ("PAPERPILOT_API_KEY", "DEEPSEEK_API_KEY")

# 状态码 → (kind, 给人看的 hint)。可重试的 kind 见 _RETRYABLE_KINDS
_STATUS_KINDS: dict[int, tuple[str, str]] = {
    400: ("bad_request", "请求体格式错误：检查 messages / max_tokens / response_format"),
    401: ("auth", "API key 错误或未授权：检查环境变量 PAPERPILOT_API_KEY 或 DEEPSEEK_API_KEY"),
    402: ("balance", "账号余额不足：请前往模型平台充值后重试"),
    422: ("invalid_param", "参数错误：按接口返回的错误信息修改请求参数"),
    429: ("rate_limit", "触发限速（TPM/RPM）：调低 ai.max_concurrency、减小批量，或稍后重试"),
    500: ("server_error", "服务端故障：稍后重试；持续出现请联系模型平台"),
    503: ("unavailable", "服务繁忙：稍后重试"),
}
_RETRYABLE_KINDS = {"rate_limit", "server_error", "unavailable", "timeout"}


def resolve_api_key(explicit: str | None = None) -> str | None:
    """显式配置优先，否则按顺序查环境变量。"""
    if explicit and explicit.strip():
        return explicit.strip()
    for env in API_KEY_ENVS:
        value = os.environ.get(env)
        if value and value.strip():
            return value.strip()
    return None


def unified_available(api_key: str | None = None) -> bool:
    """unified 档可用 = 拿得到 API key。"""
    return resolve_api_key(api_key) is not None


class UnifiedAIAdapter:
    """OpenAI 兼容 chat/completions 客户端，实现领域契约 LLMPort。"""

    name = "unified-ai"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleeper=time.sleep,
    ) -> None:
        key = resolve_api_key(api_key)
        if not key:
            raise UnifiedAINotReady(
                "未配置 API key",
                hint=f"设置环境变量 {' 或 '.join(API_KEY_ENVS)}，"
                "或在 config/settings.yaml 的 ai.api_key 填写；"
                "未配置时 pipeline 会自动降级到 heuristic 档",
            )
        self._api_key = key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self._transport = transport
        self._sleep = sleeper
        self._client: httpx.Client | None = None

    # ---------------------------------------------------------------- HTTP
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> UnifiedAIAdapter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 主入口
    def complete(
        self,
        *,
        messages: Sequence[Message],
        temperature: float = 0.2,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> LLMResult:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            # DeepSeek JSON Output：response_format=json_object（prompt 须含 json 字样，
            # 我们的系统提示已满足；max_tokens 由调用方给足，防截断）
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._post_once(payload)
            except LLMHTTPError as exc:
                last_error = exc
                if exc.kind not in _RETRYABLE_KINDS or attempt == self.max_retries:
                    raise
                backoff = min(2.0**attempt, 8.0)
                logger.warning("LLM %s（第 %d 次），%.1fs 后重试", exc.kind, attempt, backoff)
                self._sleep(backoff)
            except LLMTimeoutError as exc:
                last_error = exc
                if attempt == self.max_retries:
                    raise
                backoff = min(2.0**attempt, 8.0)
                logger.warning("LLM 超时（第 %d 次），%.1fs 后重试", attempt, backoff)
                self._sleep(backoff)
        raise last_error  # pragma: no cover - 循环内必然已 return/raise

    # ---------------------------------------------------------------- 单次请求
    def _post_once(self, payload: dict) -> LLMResult:
        started = time.perf_counter()
        try:
            resp = self._http().post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"LLM 请求超时（{self.timeout}s）",
                hint="调大 ai.timeout_seconds，或缩小批量/摘要长度",
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMHTTPError(
                f"LLM 网络错误: {exc}",
                status=0,
                kind="network",
                hint="检查网络/代理与 ai.base_url 是否可达",
            ) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if resp.status_code != 200:
            raise self._http_error(resp)

        try:
            data = resp.json()
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AIParseError(
                "LLM 响应结构不符合 OpenAI 兼容协议",
                hint="检查 ai.base_url 是否指向兼容 /chat/completions 的服务",
                raw=resp.text[:200],
            ) from exc

        if not text.strip():
            # DeepSeek JSON Output 已知问题：偶发空 content
            raise AIParseError(
                "LLM 返回空 content",
                hint="JSON Output 偶发空 content：修改 prompt 表述后重试，"
                "或把 ai.provider 临时切到 heuristic",
            )

        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            model=str(data.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
        )

    def _http_error(self, resp: httpx.Response) -> LLMHTTPError:
        status = resp.status_code
        kind, hint = _STATUS_KINDS.get(status, ("http_error", "按接口返回的错误信息处理"))
        # 上下文超长：DeepSeek 走 400 + 文案含 context/length
        body = ""
        try:
            body = resp.json().get("error", {}).get("message", "") or ""
        except ValueError:
            body = resp.text[:200]
        if status == 400 and any(k in body.lower() for k in ("context", "length", "too long")):
            kind, hint = (
                "context_length",
                "输入超出模型上下文：调小 ai.max_abstract_chars 或减小打分批量",
            )
        return LLMHTTPError(
            f"LLM HTTP {status}: {body[:200]}", status=status, kind=kind, hint=hint
        )
