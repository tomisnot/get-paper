"""中性能力层测试：自描述、统一信封、读写归因、体积闸、出站信号、CLI 参数解析。

对应 docs/SPEC.md §3–§4（能力清单 + 外部调用形式）与 docs/PRINCIPLES.md 信条 9。
"""

from __future__ import annotations

from paperpilot.capabilities import invoke, specs
from paperpilot.capabilities.base import gate


# ------------------------------------------------------------------ 自描述 & 调度
def test_specs_are_self_describing(container):
    specs_list = specs(container)
    by_name = {s["name"]: s for s in specs_list}
    assert {"search_papers", "get_paper", "fetch_paper_by_id", "run_pipeline", "undo"} <= set(by_name)
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


# ------------------------------------------------------------------ M0 出站信号（漏斗记账，不落本地文件）
def test_record_signal_is_append_only_event(container, sample_papers):
    """能红：信号进事件总线可查；不可逆（undo 不抓它）。"""
    container.repo.upsert_papers(sample_papers[:1], actor="human", reason="seed")
    pid = sample_papers[0].arxiv_id
    container.repo.record_signal(pid, "download", source="measured",
                                 actor="human", reason="直下跳转")
    ev = invoke(container, "get_activity", op="signal:download")
    assert ev["ok"] and ev["events_count"] == 1
    assert ev["events"][-1]["after"]["source"] == "measured"
    seq = ev["events"][-1]["seq"]
    u = invoke(container, "undo", seq=seq, actor="human")
    assert u["ok"] is False and u["error"]["kind"] == "irreversible"   # 信号本身不可回滚


def test_pdf_route_redirects_records_signal_and_writes_no_file(container, sample_papers):
    """能红（M0 主案）：/pdf 路由记 download 信号并 302 到 arXiv；不产生任何本地文件，
    pdf_dir 已整体消失（红证：属性不存在）。"""
    from fastapi.testclient import TestClient

    from paperpilot.app.web import create_app

    container.repo.upsert_papers(sample_papers[:1], actor="human", reason="seed")
    pid = sample_papers[0].arxiv_id
    client = TestClient(create_app(container, None), follow_redirects=False)

    r = client.get(f"/papers/{pid}/pdf")
    assert r.status_code in (302, 307)
    assert "arxiv.org/pdf" in r.headers["location"]
    assert invoke(container, "get_activity", op="signal:download")["events_count"] == 1

    g = client.get(f"/papers/{pid}/go")
    assert g.status_code in (302, 307) and "arxiv.org/abs" in g.headers["location"]
    assert invoke(container, "get_activity", op="signal:outbound")["events_count"] == 1

    assert not hasattr(container.settings, "pdf_dir")          # 本地 PDF 体系已拆


def test_record_signal_bad_paper_still_logged_not_raised(container):
    """不误报/不阻断：库内无此篇时路由照常 302（信号写失败不拦用户），但跳转不断。"""
    from fastapi.testclient import TestClient

    from paperpilot.app.web import create_app

    client = TestClient(create_app(container, None), follow_redirects=False)
    r = client.get("/papers/nope.0000/pdf")   # 库里没有：仍 302 到构造地址
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "https://arxiv.org/pdf/nope.0000"


# ------------------------------------------------------------------ 体积闸
def test_gate_truncates_long_lists():
    obj = {"ok": True, "papers": [{"i": i} for i in range(50)]}
    gated = gate(obj)
    assert len(gated["papers"]) == 20
    assert gated["_papers_truncated"]["total"] == 50


# ------------------------------------------------------------------ infra: 无本地下载（M0 后 download_pdf 已退役）


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
