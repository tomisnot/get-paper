"""中性能力层测试：自描述、统一信封、读写归因、体积闸、download_paper、CLI 参数解析。

对应 docs/SPEC.md §3–§4（能力清单 + 外部调用形式）与 docs/PRINCIPLES.md 信条 9。
"""

from __future__ import annotations

import httpx
import pytest

from paperpilot.capabilities import invoke, specs
from paperpilot.capabilities.base import gate
from paperpilot.infra.arxiv import ArxivClient, ArxivError


# ------------------------------------------------------------------ 自描述 & 调度
def test_specs_are_self_describing(container):
    specs_list = specs(container)
    by_name = {s["name"]: s for s in specs_list}
    assert {"search_papers", "get_paper", "download_paper", "run_pipeline", "undo"} <= set(by_name)
    assert by_name["download_paper"]["kind"] == "write"
    assert by_name["search_papers"]["kind"] == "read"
    # 入参 schema 自动从签名推导
    assert "query" in by_name["search_papers"]["params"]
    assert by_name["search_papers"]["params"]["limit"]["type"] == "integer"
    assert by_name["get_paper"]["params"]["arxiv_id"]["required"] is True


def test_invoke_unknown_tool_returns_teachable_error(container):
    result = invoke(container, "no_such_tool")
    assert result["ok"] is False
    assert result["error"]["kind"] == "unknown_tool"
    assert result["error"]["hint"]
    assert "search_papers" in result["error"]["suggest"]


def test_invoke_bad_params(container):
    result = invoke(container, "get_paper")  # 缺 arxiv_id
    assert result["ok"] is False
    assert result["error"]["kind"] == "bad_params"


# ------------------------------------------------------------------ 只读能力
def test_list_topics(container):
    result = invoke(container, "list_topics")
    assert result["ok"] is True
    assert len(result["topics"]) == 3


def test_search_and_get_paper(container, sample_papers):
    container.repo.upsert_papers(sample_papers, actor="human", reason="seed")
    pid = sample_papers[0].arxiv_id

    search = invoke(container, "search_papers", query="")
    assert search["ok"] is True and search["count"] >= 1

    detail = invoke(container, "get_paper", arxiv_id=pid)
    assert detail["ok"] is True
    assert detail["paper"]["arxiv_id"] == pid
    assert detail["paper"]["local_pdf"] is None

    missing = invoke(container, "get_paper", arxiv_id="nope.999")
    assert missing["ok"] is False and missing["error"]["kind"] == "not_found"


# ------------------------------------------------------------------ 写能力：归因 + 可逆
def test_write_records_actor_and_undo(container, sample_papers):
    container.repo.upsert_papers(sample_papers, actor="human", reason="seed")
    pid = sample_papers[0].arxiv_id

    r = invoke(container, "mark_read", arxiv_id=pid, read=True, actor="ai", reason="测试")
    assert r["ok"] is True

    events = invoke(container, "get_activity", actor="ai", op="set_read")
    assert events["events_count"] >= 1

    u = invoke(container, "undo", seq=0, actor="human", reason="撤销测试")
    assert u["ok"] is True


# ------------------------------------------------------------------ download_paper
def test_download_paper_idempotent_and_archived(container, sample_papers, monkeypatch):
    container.repo.upsert_papers(sample_papers[:1], actor="human", reason="seed")
    pid = sample_papers[0].arxiv_id

    import paperpilot.capabilities.tools as tools_mod

    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def download_pdf(self, url, dest, **k):
            calls["n"] += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4 fake")
            return 13

        def close(self):
            pass

    monkeypatch.setattr(tools_mod, "ArxivClient", FakeClient)

    r1 = invoke(container, "download_paper", arxiv_id=pid, actor="human", reason="下载")
    assert r1["ok"] is True and r1["cached"] is False and r1["bytes"] == 13
    assert (container.settings.pdf_dir / f"{pid}.pdf").exists()

    r2 = invoke(container, "download_paper", arxiv_id=pid, actor="human", reason="再下")
    assert r2["ok"] is True and r2["cached"] is True
    assert calls["n"] == 1  # 幂等：未重复下载

    detail = invoke(container, "get_paper", arxiv_id=pid)
    assert detail["paper"]["local_pdf"] is not None

    ev = invoke(container, "get_activity", op="download_paper")
    assert ev["events_count"] >= 1


def test_download_paper_not_found(container):
    r = invoke(container, "download_paper", arxiv_id="nope.1")
    assert r["ok"] is False and r["error"]["kind"] == "not_found"


# ------------------------------------------------------------------ 体积闸
def test_gate_truncates_long_lists():
    obj = {"ok": True, "papers": [{"i": i} for i in range(50)]}
    gated = gate(obj)
    assert len(gated["papers"]) == 20
    assert gated["_papers_truncated"]["total"] == 50


# ------------------------------------------------------------------ infra: download_pdf
def test_arxiv_download_pdf_streams_to_file(tmp_path):
    payload = b"%PDF-1.4 real-ish bytes"

    def handler(request):
        return httpx.Response(200, content=payload)

    client = ArxivClient(
        cache_dir=tmp_path / "cache", min_interval=0.0,
        transport=httpx.MockTransport(handler), sleeper=lambda s: None,
    )
    dest = tmp_path / "pdfs" / "x.pdf"
    n = client.download_pdf("https://arxiv.org/pdf/x", dest)
    client.close()
    assert n == len(payload)
    assert dest.read_bytes() == payload


def test_arxiv_download_pdf_retries_then_fails(tmp_path):
    def handler(request):
        return httpx.Response(500)

    client = ArxivClient(
        cache_dir=tmp_path / "cache", min_interval=0.0, retries=2,
        transport=httpx.MockTransport(handler), sleeper=lambda s: None,
    )
    with pytest.raises(ArxivError):
        client.download_pdf("https://arxiv.org/pdf/x", tmp_path / "y.pdf")
    client.close()


# ------------------------------------------------------------------ CLI 参数解析（按 schema 强制类型）
def test_coerce_value_by_type():
    from paperpilot.app.cli import _coerce_value

    assert _coerce_value("5", "integer") == 5
    assert _coerce_value("0.6", "number") == 0.6
    assert _coerce_value("true", "boolean") is True
    assert _coerce_value("[1,2]", "array") == [1, 2]
    # 关键回归：string 型不做 JSON 解析，arxiv_id 不被当浮点
    assert _coerce_value("1706.03762", "string") == "1706.03762"


def test_coerce_params_uses_tool_schema(container):
    from paperpilot.app.cli import _coerce_params
    from paperpilot.capabilities import registry_for

    spec = registry_for(container).get("get_references")
    out = _coerce_params(spec, ["arxiv_id=1706.03762", "limit=5", "sort_by_citations=false"])
    assert out["arxiv_id"] == "1706.03762"  # 字符串，不是 1706.03762 浮点
    assert out["limit"] == 5
    assert out["sort_by_citations"] is False
