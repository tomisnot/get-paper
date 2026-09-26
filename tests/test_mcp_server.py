"""MCP 语义通道测试：工具函数直调（不起 HTTP），覆盖 AI 分段协作全流程。

对齐 Energy Level 的判据姿势：create_server 返回 tools_dict，直接调用即走
与 DSH 里 AI 调用完全相同的代码路径。
"""

from __future__ import annotations

import json

from paperpilot.app.container import build_container
from paperpilot.infra.arxiv import parse_atom
from paperpilot.mcp_server import _gate, create_server, read_port_file, write_port_file

from .conftest import SAMPLE_XML, make_settings


def _container(tmp_path):
    settings = make_settings(tmp_path / "data")
    container = build_container(settings)
    container.repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")))
    return container


def _tools(tmp_path):
    container = _container(tmp_path)
    _srv, tools = create_server(container)
    return container, tools


def _call(tools, tool, **kwargs):
    return json.loads(tools[tool](**kwargs))


# ---------------------------------------------------------------- 只读面
def test_list_topics_and_search(tmp_path):
    _container, tools = _tools(tmp_path)

    out = _call(tools, "list_topics")
    assert out["ok"] and len(out["topics"]) == 3

    out = _call(tools, "search_papers", query="speculative")
    assert out["ok"] and any(p["arxiv_id"] == "2608.02444" for p in out["papers"])

    out = _call(tools, "get_paper", arxiv_id="2608.01101")
    assert out["ok"] and "Test-Time Compute" in out["paper"]["title"]

    out = _call(tools, "get_paper", arxiv_id="9999.99999")
    assert out["ok"] is False and out["error"]["kind"] == "not_found"


def test_get_digest_missing_is_teachable(tmp_path):
    _container, tools = _tools(tmp_path)
    out = _call(tools, "get_digest")
    assert out["ok"] and out["exists"] is False and "hint" in out


def test_volume_gate_truncates_with_where():
    payload = {"ok": True, "items": [{"i": i} for i in range(100)]}
    gated = _gate(payload)
    assert len(gated["items"]) == 20
    assert gated["_items_truncated"]["total"] == 100
    assert "where" in gated["_items_truncated"]


def test_port_file_roundtrip(tmp_path):
    path = tmp_path / ".mcp-port"
    write_port_file(8811, path=str(path))
    assert read_port_file(path=str(path)) == 8811


# ---------------------------------------------------------------- AI 分段协作全流程
def test_prepare_submit_finalize_flow(tmp_path):
    container, tools = _tools(tmp_path)

    # 阶段 1：候选 + 基线分
    prepared = _call(tools, "prepare_review")
    assert prepared["ok"] is True
    assert prepared["n_candidates"] == 9
    assert prepared["candidates"], "候选不应为空"
    date_str = prepared["date"]
    first = prepared["candidates"][0]
    assert "baseline" in first and first["abstract"]  # 交给 AI 的最小充分信息
    assert len(first["abstract"]) <= 700

    # 阶段 2：AI 评审（全量提交；混一条脏数据验证容错）
    reviews = [
        {
            "arxiv_id": c["arxiv_id"],
            "score": 0.95 if i == 0 else 0.3,
            "label": "must_read" if i == 0 else "skip",
            "reason": "AI 判定：与主题强相关" if i == 0 else "相关性不足",
            "tags": ["reasoning"],
            **(
                {"summary": {
                    "tldr": "TLDR", "problem": "P", "method": "M",
                    "results": "R", "novelty": "N", "keywords": ["k1"],
                }} if i == 0 else {}
            ),
        }
        for i, c in enumerate(prepared["candidates"])
    ]
    reviews.append({"arxiv_id": "0000.00000", "score": 0.9, "label": "worth"})  # 不在候选内
    submitted = _call(tools, "submit_review", date_str=date_str, reviews=reviews)
    assert submitted["ok"] and submitted["accepted"] == len(prepared["candidates"])
    assert len(submitted["rejected"]) == 1

    status = _call(tools, "review_status", date_str=date_str)
    assert status["status"] == "reviewed" and status["reviewed"] == len(prepared["candidates"])

    # 阶段 3：组装简报（AI 评审优先）
    finalized = _call(tools, "finalize_briefing", date_str=date_str)
    assert finalized["ok"] and finalized["selected"] >= 1
    assert finalized["reused"] is False

    digest = _call(tools, "get_digest", date_str=date_str, full=True)
    assert digest["exists"] and "markdown" in digest
    assert digest["stats"]["ai_provider"] == "dsh-review"
    assert digest["items"][0]["summary"]["tldr"] == "TLDR"  # AI 交的精读被采用

    # 幂等：不 force 再 finalize → 复用
    again = _call(tools, "finalize_briefing", date_str=date_str)
    assert again["reused"] is True


def test_submit_review_without_prepare_is_teachable(tmp_path):
    _container, tools = _tools(tmp_path)
    out = _call(tools, "submit_review", date_str="2020-01-01", reviews=[])
    assert out["ok"] is False
    assert out["error"]["kind"] == "parse"
    assert "prepare_review" in out["error"]["hint"]


def test_finalize_without_review_is_teachable(tmp_path):
    _container, tools = _tools(tmp_path)
    out = _call(tools, "finalize_briefing", date_str="2020-01-01")
    assert out["ok"] is False and "prepare_review" in out["error"]["hint"]


def test_baseline_fallback_when_ai_silent(tmp_path):
    """AI 只评一部分：未评的篇目用基线分，流程照常完成。"""
    _container, tools = _tools(tmp_path)
    prepared = _call(tools, "prepare_review")
    date_str = prepared["date"]
    only = prepared["candidates"][0]
    _call(tools, "submit_review", date_str=date_str, reviews=[{
        "arxiv_id": only["arxiv_id"], "score": 0.99, "label": "must_read", "reason": "必读",
    }])
    finalized = _call(tools, "finalize_briefing", date_str=date_str)
    assert finalized["ok"] and finalized["selected"] >= 1


# ---------------------------------------------------------------- 写入面
def test_add_topic_and_toggle(tmp_path):
    container, tools = _tools(tmp_path)

    out = _call(tools, "add_topic", name="多模态", keywords="CLIP, vision-language",
                categories="cs.CV", description="图文理解")
    assert out["ok"] and any(t.name == "多模态" for t in container.settings.topics)
    # 写回 YAML（唯一事实源）
    assert "多模态" in container.settings.config_path.read_text(encoding="utf-8")

    out = _call(tools, "add_topic", name="多模态")
    assert out["ok"] is False and out["error"]["kind"] == "duplicate"
    assert out["error"]["suggest"]  # 可教学：给出现有主题

    out = _call(tools, "set_topic_enabled", name="多模态", enabled=False)
    assert out["ok"] and out["enabled"] is False

    out = _call(tools, "set_topic_enabled", name="不存在", enabled=True)
    assert out["ok"] is False and out["error"]["kind"] == "unknown_topic"


def test_reading_state_tools(tmp_path):
    _container, tools = _tools(tmp_path)
    assert _call(tools, "mark_read", arxiv_id="2608.01101")["ok"]
    assert _call(tools, "star_paper", arxiv_id="2608.01101")["star"] is True
    assert _call(tools, "skip_paper", arxiv_id="2608.01101")["ok"]
    assert _call(tools, "add_note", arxiv_id="2608.01101", content="笔记")["ok"]
    detail = _call(tools, "get_paper", arxiv_id="2608.01101")
    assert detail["notes"] == ["笔记"] and detail["reading"]["star"] is True


def test_run_pipeline_one_shot(tmp_path):
    container, tools = _tools(tmp_path)
    out = _call(tools, "run_pipeline", force=True)
    assert out["ok"] and out["selected"] >= 1
    # heuristic 档：无降级、无 AI 调用
    assert out["degraded"] == []


def test_get_activity(tmp_path):
    _container, tools = _tools(tmp_path)
    _call(tools, "run_pipeline", force=True)
    out = _call(tools, "get_activity", days=7)
    assert out["ok"] and "counts_by_status" in out and "recent_briefings" in out
