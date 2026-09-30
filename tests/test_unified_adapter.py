"""UnifiedAIAdapter（OpenAI 兼容客户端）测试：httpx.MockTransport 假接口，不联网。

覆盖 DESIGN.md §4.3 验收清单：
- json_mode → response_format=json_object，且带 Authorization 头
- 错误分类（401 认证 / 402 余额 / 429 限速重试 / 400 上下文超长 / 空 content）
- 429/5xx/超时指数退避重试；不可重试的错误立即抛
- token 用量回填（ai_calls 记账依赖）
"""

from __future__ import annotations

import json

import httpx
import pytest

from paperpilot.domain.models import Message
from paperpilot.infra.ai.errors import (
    AIParseError,
    LLMHTTPError,
    LLMTimeoutError,
    UnifiedAINotReady,
)
from paperpilot.infra.ai.unified import UnifiedAIAdapter, resolve_api_key

_MESSAGES = [Message(role="system", content="输出 JSON"), Message(role="user", content="评一篇")]


def _ok(content: str, *, model: str = "deepseek-chat", pt: int = 10, ct: int = 20) -> dict:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct},
    }


def _adapter(handler, **kw):
    """造一个指向假接口的 adapter（不碰网络、不睡真觉）。"""
    transport = httpx.MockTransport(handler)
    return UnifiedAIAdapter(
        "test-key",
        base_url="https://api.test.local",
        model=kw.pop("model", "deepseek-chat"),
        max_retries=kw.pop("max_retries", 3),
        transport=transport,
        sleeper=lambda _s: None,  # 测试不真睡
        **kw,
    )


# ---------------------------------------------------------------- 成功路径
def test_complete_success_and_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok('{"scores": []}'))

    adapter = _adapter(handler)
    result = adapter.complete(messages=_MESSAGES, temperature=0.1, json_mode=True)

    assert result.text == '{"scores": []}'
    assert result.model == "deepseek-chat"
    assert (result.prompt_tokens, result.completion_tokens) == (10, 20)
    assert result.latency_ms >= 0
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["stream"] is False
    assert seen["body"]["temperature"] == 0.1


def test_complete_without_json_mode_omits_response_format():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "response_format" not in body
        return httpx.Response(200, json=_ok("plain text"))

    result = _adapter(handler).complete(messages=_MESSAGES)
    assert result.text == "plain text"


# ---------------------------------------------------------------- 错误分类
@pytest.mark.parametrize(
    "status,kind",
    [(400, "bad_request"), (401, "auth"), (402, "balance"), (422, "invalid_param"), (500, "server_error"), (503, "unavailable")],
)
def test_http_error_kinds(status, kind):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"error": {"message": "boom"}})

    with pytest.raises(LLMHTTPError) as exc_info:
        _adapter(handler).complete(messages=_MESSAGES)
    assert exc_info.value.kind == kind
    assert exc_info.value.to_dict()["hint"]  # 可教学：必须带 hint


def test_auth_error_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    with pytest.raises(LLMHTTPError) as exc_info:
        _adapter(handler).complete(messages=_MESSAGES)
    assert calls["n"] == 1  # 不可重试的错误只发一次
    assert "PAPERPILOT_API_KEY" in exc_info.value.to_dict()["hint"]


def test_context_length_error_detected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "maximum context length exceeded"}})

    with pytest.raises(LLMHTTPError) as exc_info:
        _adapter(handler).complete(messages=_MESSAGES)
    assert exc_info.value.kind == "context_length"
    assert "max_abstract_chars" in exc_info.value.to_dict()["hint"]


def test_rate_limit_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "too many requests"}})
        return httpx.Response(200, json=_ok('{"ok": true}'))

    result = _adapter(handler).complete(messages=_MESSAGES)
    assert result.text == '{"ok": true}'
    assert calls["n"] == 2  # 429 触发一次重试


def test_rate_limit_exhausts_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "busy"}})

    with pytest.raises(LLMHTTPError) as exc_info:
        _adapter(handler, max_retries=2).complete(messages=_MESSAGES)
    assert exc_info.value.kind == "unavailable"


def test_timeout_raises_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(LLMTimeoutError):
        _adapter(handler).complete(messages=_MESSAGES)


# ---------------------------------------------------------------- 输出纪律
def test_empty_content_is_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok(""))

    with pytest.raises(AIParseError) as exc_info:
        _adapter(handler).complete(messages=_MESSAGES, json_mode=True)
    assert exc_info.value.kind == "parse"
    assert "空 content" in exc_info.value.to_dict()["hint"]


def test_malformed_response_structure_is_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(AIParseError):
        _adapter(handler).complete(messages=_MESSAGES)


# ---------------------------------------------------------------- key 解析
def test_resolve_api_key_priority(monkeypatch):
    assert resolve_api_key("explicit") == "explicit"
    monkeypatch.setenv("PAPERPILOT_API_KEY", "from-env")
    assert resolve_api_key(None) == "from-env"
    monkeypatch.delenv("PAPERPILOT_API_KEY")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-deepseek")
    assert resolve_api_key(None) == "from-deepseek"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert resolve_api_key(None) is None


def test_adapter_requires_key():
    with pytest.raises(UnifiedAINotReady):
        UnifiedAIAdapter(model="deepseek-chat")


# ---------------------------------------------------------------- 端到端（ranker 走真 prompt）
def test_llm_ranker_end_to_end_through_adapter():
    payload = {
        "scores": [
            {"i": 1, "score": 0.9, "label": "must_read", "reason": "高度相关", "tags": ["reasoning"]},
            {"i": 2, "score": 0.4, "label": "skip", "reason": "不相关", "tags": []},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        assert "JSON" in body["messages"][0]["content"]  # json_mode 前提：prompt 含 json
        return httpx.Response(200, json=_ok(json.dumps(payload, ensure_ascii=False)))

    from types import SimpleNamespace

    from paperpilot.infra.ai import LLMRanker

    adapter = _adapter(handler)
    ranker = LLMRanker(adapter)
    papers = [
        SimpleNamespace(id=1, arxiv_id="a", title="T1", abstract="reasoning chain-of-thought",
                        authors=["X"], primary_category="cs.CL"),
        SimpleNamespace(id=2, arxiv_id="b", title="T2", abstract="traffic forecasting",
                        authors=["Y"], primary_category="cs.LG"),
    ]
    # 参数已正名：`interest`（画像口径）——LLMRanker 只读 name/description/keywords
    profile = SimpleNamespace(name="推理", description="", keywords=["reasoning"], authors=[])
    scores = ranker.score_batch(papers=papers, interest=profile, run_id="t")
    assert [round(s.score, 2) for s in scores] == [0.9, 0.4]
    assert scores[0].label == "must_read"
