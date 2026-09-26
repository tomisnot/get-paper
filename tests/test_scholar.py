"""引文分析能力测试：Semantic Scholar 客户端（MockTransport，不联网）+ 能力层封装。

对应 docs/SPEC.md「专项调查 / 引文分析」方向：往前扒技术起源的地基 = 参考文献按引用数排序。
"""

from __future__ import annotations

import httpx
import pytest

from paperpilot.capabilities import invoke
from paperpilot.infra.scholar import (
    ScholarError,
    SemanticScholarClient,
    arxiv_ext_id,
)


def _client(tmp_path, handler, **kw) -> SemanticScholarClient:
    return SemanticScholarClient(
        cache_dir=tmp_path / "s2", min_interval=0.0,
        transport=httpx.MockTransport(handler), sleeper=lambda s: None, **kw
    )


# ------------------------------------------------------------------ ID 包装
def test_arxiv_ext_id():
    assert arxiv_ext_id("1706.03762") == "arXiv:1706.03762"
    assert arxiv_ext_id("arXiv:1706.03762") == "arXiv:1706.03762"
    assert arxiv_ext_id("DOI:10.1/x") == "DOI:10.1/x"


# ------------------------------------------------------------------ 客户端
def test_client_paper_parses(tmp_path):
    payload = {
        "paperId": "abc", "title": "Attention Is All You Need", "year": 2017,
        "venue": "NeurIPS", "citationCount": 100000, "referenceCount": 40,
        "influentialCitationCount": 5000,
        "externalIds": {"ArXiv": "1706.03762", "DOI": "10.x"},
        "tldr": {"model": "v", "text": "提出 Transformer"},
    }
    c = _client(tmp_path, lambda request: httpx.Response(200, json=payload))
    p = c.paper("arXiv:1706.03762")
    c.close()
    assert p["citationCount"] == 100000
    assert p["tldr"]["text"] == "提出 Transformer"


def test_client_references_returns_edges(tmp_path):
    payload = {"offset": 0, "data": [
        {"citedPaper": {"paperId": "r1", "title": "Old", "year": 2014,
                         "citationCount": 5000, "externalIds": {"ArXiv": "1401.1"},
                         "tldr": {"text": "seq2seq"}},
         "intents": ["method"], "isInfluential": True},
        {"citedPaper": {"paperId": "r2", "title": "Newer", "year": 2016,
                         "citationCount": 500, "externalIds": {}},
         "intents": ["background"], "isInfluential": False},
    ]}
    c = _client(tmp_path, lambda request: httpx.Response(200, json=payload))
    items = c.references("arXiv:1706.03762", limit=10)
    c.close()
    assert len(items) == 2
    assert items[0]["citedPaper"]["title"] == "Old"


def test_client_404_raises_no_retry(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"error": "not found"})

    c = _client(tmp_path, handler, retries=3)
    with pytest.raises(ScholarError):
        c.paper("arXiv:nope")
    c.close()
    assert calls["n"] == 1  # 404 不重试


def test_client_400_raises_no_retry(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad fields"})

    c = _client(tmp_path, handler, retries=3)
    with pytest.raises(ScholarError):
        c.references("arXiv:x", limit=2)
    c.close()
    assert calls["n"] == 1  # 400 客户端错误不重试


def test_client_retries_on_429(tmp_path):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] < 2:
            return httpx.Response(429)
        return httpx.Response(200, json={"paperId": "x", "citationCount": 7})

    c = _client(tmp_path, handler, retries=3)
    p = c.paper("arXiv:x")
    c.close()
    assert p["citationCount"] == 7 and state["n"] == 2


def test_client_caches(tmp_path):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(200, json={"paperId": "x", "citationCount": state["n"]})

    c = _client(tmp_path, handler)
    a = c.paper("arXiv:x")
    b = c.paper("arXiv:x")
    c.close()
    assert state["n"] == 1 and a == b  # 第二次命中缓存


# ------------------------------------------------------------------ 能力层
class _FakeScholar:
    """替身：不联网，返回固定引文数据。"""

    def __init__(self, *a, **k):
        pass

    def paper(self, ext_id, fields=""):
        return {"paperId": "s2id", "title": "T", "year": 2017, "venue": "NeurIPS",
                "citationCount": 100000, "referenceCount": 40,
                "influentialCitationCount": 5000,
                "externalIds": {"DOI": "10.x"}, "tldr": {"text": "提出 Transformer"}}

    def references(self, ext_id, limit=50, fields=""):
        return [
            {"citedPaper": {"paperId": "r1", "title": "Low", "year": 2016,
                             "citationCount": 10, "externalIds": {"ArXiv": "a"},
                             "tldr": {"text": "x"}},
             "intents": ["background"], "isInfluential": False},
            {"citedPaper": {"paperId": "r2", "title": "Seminal", "year": 2014,
                             "citationCount": 9000, "externalIds": {"ArXiv": "b"},
                             "tldr": {"text": "y"}},
             "intents": ["method"], "isInfluential": True},
        ]

    def citations(self, ext_id, limit=50, fields=""):
        return [{"citingPaper": {"paperId": "c1", "title": "Followup", "year": 2019,
                                 "citationCount": 300, "externalIds": {}},
                 "intents": ["result"], "isInfluential": False}]

    def close(self):
        pass


def _patch_scholar(monkeypatch):
    import paperpilot.capabilities.tools as tools_mod
    monkeypatch.setattr(tools_mod, "SemanticScholarClient", _FakeScholar)


def test_cap_paper_metrics(container, monkeypatch):
    _patch_scholar(monkeypatch)
    r = invoke(container, "paper_metrics", arxiv_id="1706.03762")
    assert r["ok"] is True
    assert r["citation_count"] == 100000
    assert r["tldr"] == "提出 Transformer"
    assert r["doi"] == "10.x"


def test_cap_get_references_sorts_by_citations(container, monkeypatch):
    _patch_scholar(monkeypatch)
    r = invoke(container, "get_references", arxiv_id="1706.03762", limit=10)
    assert r["ok"] is True and r["count"] == 2
    # 起源候选（高被引、年份早）排在最前
    assert r["references"][0]["title"] == "Seminal"
    assert r["references"][0]["citation_count"] == 9000
    assert r["references"][0]["intents"] == ["method"]
    assert r["references"][0]["influential"] is True


def test_cap_get_citations(container, monkeypatch):
    _patch_scholar(monkeypatch)
    r = invoke(container, "get_citations", arxiv_id="1706.03762", limit=10)
    assert r["ok"] is True and r["count"] == 1
    assert r["citations"][0]["title"] == "Followup"


def test_cap_scholar_error_is_teachable(container, monkeypatch):
    import paperpilot.capabilities.tools as tools_mod

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def references(self, *a, **k):
            raise ScholarError("HTTP 429")

        def close(self):
            pass

    monkeypatch.setattr(tools_mod, "SemanticScholarClient", _Boom)
    r = invoke(container, "get_references", arxiv_id="x")
    assert r["ok"] is False
    assert r["error"]["kind"] == "scholar_unavailable"
    assert r["error"]["hint"]


def test_specs_include_citation_tools(container):
    from paperpilot.capabilities import specs
    names = {s["name"] for s in specs(container)}
    assert {"paper_metrics", "get_references", "get_citations"} <= names
