"""mecha v2 适配层测试：工具面直调（进程内）+ 真 MCP 投影端到端。

覆盖从旧 ``test_mcp_server.py`` 迁移的 AI 分段协作全流程（工具改名后），并补
接入指南第 3 步的**真 MCP 客户端**验收：list_tools == 注册面、required 投影不塌、
不声明 outputSchema、真调一个写工具 → journal 真落盘。

工具直调走 ``ToolRegistry.execute``（与 MCP 投影同一条 ``ToolHost.call`` 路径的
上游），返回 ``{is_error, value|error, content}``；``_call`` 把它折回能力信封形状
（``{ok, ...}`` / ``{ok:False, error:{kind,message,hint}}``）以复用旧断言。
"""

from __future__ import annotations

import asyncio
import json

from mecha.authority import Mode

from paperpilot.app.container import build_container
from paperpilot.capabilities.base import gate as volume_gate
from paperpilot.infra.arxiv import parse_atom
from paperpilot.mecha_adapter.hub import build_stack, make_host
from paperpilot.mecha_adapter.tools import TOOL_TO_CAPABILITY

from .conftest import SAMPLE_XML, make_settings

# 28 工具的模型可见名（22 能力投影 + 配置/自省/长活面 6：set_config/read_config/read_authority/submit_job/read_job/cancel_job）。
NON_CAPABILITY_TOOLS = {"set_config", "read_config", "read_authority",
                        "submit_job", "read_job", "cancel_job"}
EXPECTED_TOOLS = set(TOOL_TO_CAPABILITY) | NON_CAPABILITY_TOOLS


def _container(tmp_path):
    settings = make_settings(tmp_path / "data")
    container = build_container(settings)
    container.repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")))
    return container


def _stack(tmp_path, *, open_ai: bool = True):
    """建栈；缺省由人类侧把写权开到 AI（模拟“人开闸”）。

    ``open_ai=False`` 保留出厂 LOCKED——治理验收用（AI 绕门写应被拒）。
    """
    container = _container(tmp_path)
    stack = build_stack(container, tmp_path, "mecha")
    if open_ai:
        stack["authority"].switch_mode(Mode.AI, side="human")
    return container, stack


def _call(tools, _tool, **args):
    """直调工具 → 折回能力信封形状（成功回 value，失败回 {ok:False,error:{...}}）。"""
    res = tools.execute(_tool, args)
    if res["is_error"]:
        failure = res["error"]
        info = failure.get("info", {})
        error = {"kind": info.get("kind"), "message": failure.get("message"),
                 "hint": info.get("hint", "")}
        if info.get("suggest"):
            error["suggest"] = info["suggest"]
        return {"ok": False, "error": error}
    return res["value"]


# ---------------------------------------------------------------- 只读面
def test_query_topics_and_search(tmp_path):
    _c, stack = _stack(tmp_path)
    tools = stack["tools"]

    out = _call(tools, "query_topics")
    assert out["ok"] and len(out["topics"]) == 3

    out = _call(tools, "search_papers", query="speculative")
    assert out["ok"] and any(p["arxiv_id"] == "2608.02444" for p in out["papers"])

    out = _call(tools, "read_paper", arxiv_id="2608.01101")
    assert out["ok"] and "Test-Time Compute" in out["paper"]["title"]

    out = _call(tools, "read_paper", arxiv_id="9999.99999")
    assert out["ok"] is False and out["error"]["kind"] == "not_found"


def test_read_digest_missing_is_teachable(tmp_path):
    _c, stack = _stack(tmp_path)
    out = _call(stack["tools"], "read_digest")
    assert out["ok"] and out["exists"] is False and "hint" in out


def test_volume_gate_truncates_with_where():
    """体积闸仍由能力层统一提供（适配层不复制第二份）。"""
    payload = {"ok": True, "items": [{"i": i} for i in range(100)]}
    gated = volume_gate(payload)
    assert len(gated["items"]) == 20
    assert gated["_items_truncated"]["total"] == 100
    assert "where" in gated["_items_truncated"]


# ---------------------------------------------------------------- AI 分段协作全流程
def test_prepare_submit_finalize_flow(tmp_path):
    _c, stack = _stack(tmp_path)
    tools = stack["tools"]

    prepared = _call(tools, "prepare_review")
    assert prepared["ok"] is True
    assert prepared["n_candidates"] == 9
    assert prepared["candidates"], "候选不应为空"
    date = prepared["date"]
    first = prepared["candidates"][0]
    assert "baseline" in first and first["abstract"]
    assert len(first["abstract"]) <= 700

    reviews = [
        {
            "arxiv_id": c["arxiv_id"],
            "score": 0.95 if i == 0 else 0.3,
            "label": "must_read" if i == 0 else "skip",
            "reason": "AI 判定：与主题强相关" if i == 0 else "相关性不足",
            "tags": ["reasoning"],
            **({"summary": {"tldr": "TLDR", "problem": "P", "method": "M",
                             "results": "R", "novelty": "N", "keywords": ["k1"]}}
               if i == 0 else {}),
        }
        for i, c in enumerate(prepared["candidates"])
    ]
    reviews.append({"arxiv_id": "0000.00000", "score": 0.9, "label": "worth"})  # 不在候选内
    submitted = _call(tools, "submit_review", date=date, reviews=reviews)
    assert submitted["ok"] and submitted["accepted"] == len(prepared["candidates"])
    assert len(submitted["rejected"]) == 1

    status = _call(tools, "review_status", date=date)
    assert status["status"] == "reviewed" and status["reviewed"] == len(prepared["candidates"])

    finalized = _call(tools, "finalize_briefing", date=date)
    assert finalized["ok"] and finalized["selected"] >= 1
    assert finalized["reused"] is False

    digest = _call(tools, "read_digest", date=date, full=True)
    assert digest["exists"] and "markdown" in digest
    assert digest["stats"]["ai_provider"] == "dsh-review"
    assert digest["items"][0]["summary"]["tldr"] == "TLDR"

    again = _call(tools, "finalize_briefing", date=date)
    assert again["reused"] is True


def test_submit_review_without_prepare_is_teachable(tmp_path):
    _c, stack = _stack(tmp_path)
    out = _call(stack["tools"], "submit_review", date="2020-01-01", reviews=[])
    assert out["ok"] is False
    assert out["error"]["kind"] == "parse"
    assert "prepare_review" in out["error"]["hint"]


def test_finalize_without_review_is_teachable(tmp_path):
    _c, stack = _stack(tmp_path)
    out = _call(stack["tools"], "finalize_briefing", date="2020-01-01")
    assert out["ok"] is False and "prepare_review" in out["error"]["hint"]


def test_baseline_fallback_when_ai_silent(tmp_path):
    _c, stack = _stack(tmp_path)
    tools = stack["tools"]
    prepared = _call(tools, "prepare_review")
    date = prepared["date"]
    only = prepared["candidates"][0]
    _call(tools, "submit_review", date=date, reviews=[{
        "arxiv_id": only["arxiv_id"], "score": 0.99, "label": "must_read", "reason": "必读",
    }])
    finalized = _call(tools, "finalize_briefing", date=date)
    assert finalized["ok"] and finalized["selected"] >= 1


# ---------------------------------------------------------------- 写入面
def test_add_topic_and_toggle(tmp_path):
    container, stack = _stack(tmp_path)
    tools = stack["tools"]

    out = _call(tools, "add_topic", name="多模态", keywords="CLIP, vision-language",
                categories="cs.CV", description="图文理解")
    assert out["ok"] and any(t.name == "多模态" for t in container.settings.topics)
    assert "多模态" in container.settings.config_path.read_text(encoding="utf-8")

    out = _call(tools, "add_topic", name="多模态")
    assert out["ok"] is False and out["error"]["kind"] == "duplicate"
    assert out["error"]["suggest"]

    out = _call(tools, "set_topic_enabled", name="多模态", enabled=False)
    assert out["ok"] and out["enabled"] is False

    out = _call(tools, "set_topic_enabled", name="不存在", enabled=True)
    assert out["ok"] is False and out["error"]["kind"] == "unknown_topic"


def test_reading_state_tools(tmp_path):
    _c, stack = _stack(tmp_path)
    tools = stack["tools"]
    assert _call(tools, "mark_read", arxiv_id="2608.01101")["ok"]
    assert _call(tools, "star_paper", arxiv_id="2608.01101")["star"] is True
    assert _call(tools, "skip_paper", arxiv_id="2608.01101")["ok"]
    assert _call(tools, "add_note", arxiv_id="2608.01101", content="笔记")["ok"]
    detail = _call(tools, "read_paper", arxiv_id="2608.01101")
    assert detail["notes"] == ["笔记"] and detail["reading"]["star"] is True


def test_run_pipeline_one_shot(tmp_path):
    """run_pipeline 经 Engine.run（surface.run）跑，回执含 run_output_required 键。"""
    _c, stack = _stack(tmp_path)
    out = _call(stack["tools"], "run_pipeline", force=True)
    assert out["ok"] and out["selected"] >= 1
    assert out["degraded"] == []


def test_read_activity(tmp_path):
    _c, stack = _stack(tmp_path)
    tools = stack["tools"]
    _call(tools, "run_pipeline", force=True)
    out = _call(tools, "read_activity", days=7)
    assert out["ok"] and "counts_by_status" in out and "recent_briefings" in out


def test_write_actor_pinned_to_channel(tmp_path):
    """写工具的 actor 由通道钉死为 'ai'（模型不可见、不可改）。"""
    container, stack = _stack(tmp_path)
    tools = stack["tools"]
    # actor 不在模型可见参数里
    schema = next(s for s in tools.schemas() if s["name"] == "add_note")
    assert "actor" not in schema["parameters"]
    _call(tools, "add_note", arxiv_id="2608.01101", content="归因")
    events = container.repo.events_since(since_seq=0, actor="ai", op="add_note")
    assert events["count"] >= 1


# ---------------------------------------------------------------- 真 MCP 投影端到端
async def _drive(port: int) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://127.0.0.1:{port}/mcp"
    async with streamable_http_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            tools = (await sess.list_tools()).tools
            names = {t.name for t in tools}
            note = next((t for t in tools if t.name == "add_note"), None)
            schema_obj = getattr(note, "input_schema", None) or getattr(note, "inputSchema", None)
            props = set((schema_obj or {}).get("properties", {}))
            required = set((schema_obj or {}).get("required", []))
            has_output = any(
                (getattr(t, "output_schema", None) or getattr(t, "outputSchema", None))
                for t in tools)
            res = await asyncio.wait_for(
                sess.call_tool("add_note",
                               {"arxiv_id": "2608.01101", "content": "真 MCP 写入"}),
                timeout=60)
            text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            is_err = getattr(res, "is_error", None)
            if is_err is None:
                is_err = getattr(res, "isError", False)
            return {"names": names, "props": props, "required": required,
                    "has_output": has_output, "is_err": bool(is_err), "text": text}


def test_mcp_projection_endtoend(tmp_path):
    """接入指南第 3 步验收：真 MCP 客户端连上、列出、真调写工具、journal 落盘。"""
    container, stack = _stack(tmp_path)
    port_file = tmp_path / ".mcp-port"
    host = make_host(stack, port=0, port_file=str(port_file))
    port = host.start()
    try:
        assert port_file.read_text(encoding="utf-8").strip() == str(port)
        out = asyncio.run(_drive(port))
    finally:
        host.stop()

    # ① list_tools == 注册面（不多不少）
    assert out["names"] == EXPECTED_TOOLS
    # ② required 投影没被 **kwargs 掏空（注入来源生效）
    assert {"arxiv_id", "content"} <= out["props"]
    assert out["required"] == {"arxiv_id", "content"}
    # ③ 不声明 outputSchema（假契约比不声明更坏）
    assert out["has_output"] is False
    # ④ 真调写工具成功（非 unknown_tool）
    assert out["is_err"] is False
    body = json.loads(out["text"])
    assert body["ok"] is True
    # ⑤ journal 真落盘：域事件流出现这条 actor=ai 的 add_note
    events = container.repo.events_since(since_seq=0, actor="ai", op="add_note")
    assert events["count"] >= 1
    assert any(e.get("after", {}).get("content") == "真 MCP 写入"
               for e in events["events"])


# ================================================================ Phase 2 · 写治理
# 验收（交接文档 §7 Phase 2）：构造「AI 绕过门直写/直跑」被拒且可归因；
# 开闸后写经命令面审计落 mecha History，且 result_ref 与域实体互引。
def test_locked_denies_write_and_no_domain_change(tmp_path):
    """洞1：LOCKED 态 AI 直写被拒，且**域写根本没发生**（不是先写后拒）。

    ⚠ **这条现在验的是框架的行为**（2026-09-26 起）：框架在调 handler **之前**就
    `gate.check(channel)`（原先检查滞后于副作用，本项目只能手写前置闸自救——n=3 发现 7）。
    项目侧那份手写闸**已删**，本判据**仍绿** ⇒ 正是"拒绝发生在域写之前"由框架保证的实测证据。
    """
    container, stack = _stack(tmp_path, open_ai=False)
    tools = stack["tools"]
    out = _call(tools, "add_note", arxiv_id="2608.01101", content="绕门写")
    assert out["ok"] is False
    assert out["error"]["kind"] == "authority_locked"    # 可归因
    # 域库无这条笔记（框架的前置检查在 handler 之前 ⇒ 域写根本没进）
    detail = _call(tools, "read_paper", arxiv_id="2608.01101")
    assert detail["notes"] == []
    # mecha History 也没有审计事件（未产生副作用）
    assert not [e for e in stack["history"].events() if e.key == "command.add_note"]


def test_write_commands_declare_what_governance_needs(tmp_path):
    """⭐ **实测**三条声明（不是读代码猜）——框架两处行为变更对本项目的杀伤半径 = 0。

    框架 `2026-09-26` 那批（ADR「命令面治理闸前置与声明式 opt-in」）有两处**语义变更**，
    主代理判定"GP 不受影响"；本项目**用这条判据自证**，而不是"信它"：

    1. `parameters.required` **缺失/空 = 无必填**（原来"缺失 ⇒ 全 properties 必填"）
       ⇒ 若某条命令的 `required` 变空，模型就**可以漏参数**了。这里逐条钉"非空 + 含 reason"。
    2. `approval_required=True` **真的拦**（调用方不注入 `approval=` 即拒）
       ⇒ 本项目**没有**需要审批的命令：一旦有人加上这个声明而没接线，**所有调用会全被拒**。
       这里钉住"一条都没声明"，将来真需要审批时这条会红，逼接线（`invoke_command(approval=…)`
       + `sw.approval` 已就位）。

    另钉第三条（本次新机制**真的挂上了**）：`wants_channel=True` —— 丢了它 handler 收不到
    通道，actor 归因就崩（发现 9 复发）。三条都是"静默变化 ⇒ AI 侧行为变坏"的东西。
    """
    _container, stack = _stack(tmp_path)
    # ③ 的接线也要真：`invoke_command(approval=…)` 传的是 `sw.approval`；
    #    若它是 None，将来某条命令一写 `approval_required=True` 就会**全被拒**（fail closed）。
    assert stack["software"].approval is not None, (
        "sw.approval 为 None ⇒ 审批通道没接上；将来声明 approval_required 会 fail-closed 全拒")
    specs = {spec.name: spec for spec in stack["commands"].specs()}
    assert specs, "一条写命令都没注册 ⇒ 上面的断言会空转（R8）"
    assert set(specs) == set(stack["command_names"]), "specs() 与注册清单不一致"
    for name, spec in sorted(specs.items()):
        assert spec.wants_channel is True, (
            f"{name} 没声明 wants_channel ⇒ handler 收不到本次通道，"
            "归因会退回「装配期默认」（发现 9 复发）")
        params = dict(spec.parameters)
        assert "required" in params, f"{name} 的 parameters 里没有 required 键"
        assert params["required"], (
            f"{name} 的 required 为空 ⇒ 参数全都不必填了（发现 6 语义翻转后「空」= 无必填）")
        assert "reason" in params["required"], (
            f"{name} 没把 reason 列进 required ⇒ 域 journal 会丢掉「为什么」（发现 8）")
        assert spec.approval_required is False, (
            f"{name} 声明了 approval_required，但没人注入 approval= ⇒ 框架会 fail-closed 全拒；"
            "要么接审批通道、要么去掉这个声明")


def test_locked_denies_run(tmp_path):
    """洞2：LOCKED 态 AI 直跑每日流水线被拒，且不生成简报。"""
    _container, stack = _stack(tmp_path, open_ai=False)
    out = _call(stack["tools"], "run_pipeline", force=True)
    assert out["ok"] is False and out["error"]["kind"] == "authority_locked"


def test_ai_cannot_self_unlock(tmp_path):
    """硬纪律4：AI 侧不能把自己解锁（只有 human 侧能切模式）。"""
    _container, stack = _stack(tmp_path, open_ai=False)
    import pytest
    from mecha.errors import GateDenied
    with pytest.raises(GateDenied) as ei:
        stack["authority"].switch_mode(Mode.AI, side="ai")
    assert ei.value.kind == "authority_switch_denied"


def test_open_gate_write_audits_history_with_result_ref(tmp_path):
    """开闸后：写经命令面 → mecha History 落 command.<name> 审计 + result_ref 互引。"""
    container, stack = _stack(tmp_path)          # open_ai=True
    tools = stack["tools"]
    out = _call(tools, "add_note", arxiv_id="2608.01101", content="审计互引")
    assert out["ok"] is True
    audits = [e for e in stack["history"].events() if e.key == "command.add_note"]
    assert len(audits) == 1
    record = audits[0].value
    assert record["command"] == "add_note" and record["ok"] is True
    assert record["side_effect"] is True and record["scope"] == ["library"]
    # result_ref 与域实体互引（arxiv_id）——不放结果体
    assert record["result_ref"]["arxiv_id"] == "2608.01101"
    # 域 journal 也真落了这条（两份 journal 各自完整）
    assert container.repo.events_since(since_seq=0, actor="ai", op="add_note")["count"] >= 1


def test_set_config_gated(tmp_path):
    """标量走 gate：种子可read；LOCKED 拒写；开闸后越界拒/合法写落快照+史。"""
    _container, stack = _stack(tmp_path, open_ai=False)
    tools = stack["tools"]
    seeded = _call(tools, "read_config")
    assert seeded["ok"] and seeded["config"]["scoring.threshold"] == 0.5
    # LOCKED：set_config 被写权门拒
    denied = _call(tools, "set_config", key="scoring.threshold", value=0.0)
    assert denied["ok"] is False and denied["error"]["kind"] == "authority_locked"
    # 人类侧开闸 → AI 可写配置
    stack["authority"].switch_mode(Mode.AI, side="human")
    # 值域守卫：越界被拒（validate）
    bad = _call(tools, "set_config", key="scoring.threshold", value=5.0)
    assert bad["ok"] is False and bad["error"]["kind"] == "bad_value"
    # 合法写：落快照 + 回读 + 进 mecha History
    wrote = _call(tools, "set_config", key="scoring.max_papers", value=2)
    assert wrote["ok"] and wrote["readback"] == 2 and wrote["before"] == 8
    assert stack["gate"].snapshot["scoring.max_papers"] == 2
    assert any(e.key == "scoring.max_papers" for e in stack["history"].events())


def test_gate_config_is_authoritative_over_run(tmp_path):
    """gate 配置对 Engine.run **权威**（非装饰）：max_papers 封顶真改变入选数。

    用两个**独立栈**（各自新建容器/库）避开“同日连跑两次候选耗尽”的干扰。
    """
    _c1, s1 = _stack(tmp_path / "a")
    default = _call(s1["tools"], "run_pipeline", force=True)
    _c2, s2 = _stack(tmp_path / "b")
    _call(s2["tools"], "set_config", key="scoring.max_papers", value=1)
    assert s2["gate"].snapshot["scoring.max_papers"] == 1
    capped = _call(s2["tools"], "run_pipeline", force=True)
    assert default["ok"] and capped["ok"]
    assert default["selected"] > capped["selected"]
    assert capped["selected"] == 1


def test_unknown_config_key_is_teachable(tmp_path):
    """写不存在的配置键 → UnknownKey 教学错（不静默）。"""
    _container, stack = _stack(tmp_path)
    out = _call(stack["tools"], "set_config", key="scoring.thresholdX", value=0.5)
    assert out["ok"] is False and out["error"]["kind"] == "unknown_key"


def test_set_config_via_tool_leaves_command_audit(tmp_path):
    """⭐ 本批的全部意义：**工具面写一次配置 ⇒ 框架账上【同时】有域键事件与 `command.set_config` 审计**。

    为什么需要这条：`set_config` 曾是**唯一直写 `gate.set`** 的写工具（不过命令面）⇒
    框架账上只有"某个配置键变了"，**没有"AI 执行了一次 set_config"这条操作记录**。
    接线到命令面之后，**这条判据是唯一能证明它真的接通了**的东西。

    红证（实测留存）：**实现接通之前**跑本条 ⇒ `command.set_config` 那句**必红**。
    """
    _container, stack = _stack(tmp_path)
    out = _call(stack["tools"], "set_config", key="scoring.max_papers", value=3,
                reason="测试操作审计")
    assert out["ok"] is True and out["readback"] == 3
    keys = [e.key for e in stack["history"].events()]
    assert "scoring.max_papers" in keys, "域键事件丢了（gate.set 那条路必须还在）"
    assert "command.set_config" in keys, (
        "框架账上没有 `command.set_config` ⇒ set_config 没走命令面（本批的全部意义）")


def test_read_config_stays_ungated(tmp_path):
    """对偶：**`read_config` 仍然不经门**（别顺手把只读那半也塞进命令面）。

    LOCKED（出厂）下也能读快照，且 History 里**不出现** `command.read_config`——
    D-4：只读路径不经门，也不产生操作审计。
    """
    _container, stack = _stack(tmp_path, open_ai=False)        # 出厂 LOCKED
    out = _call(stack["tools"], "read_config")
    assert out["ok"] is True and out["config"]["scoring.threshold"] == 0.5
    keys = [e.key for e in stack["history"].events()]
    assert "command.read_config" not in keys, "read_config 不该有命令审计（它不经门）"


# ======================================================== 设计师复审回归（三项修复）
def test_write_reason_reaches_domain_journal(tmp_path):
    """复审①：操作者的 reason 必须落进**域 journal**（repo.events），不只是 mecha 审计。

    能红：若 reason 未在命令 required 里声明，会被 RESERVED_ARGS 剥掉 → 域 journal
    的 reason 为空，此断言失败。
    """
    container, stack = _stack(tmp_path)
    tools = stack["tools"]
    _call(tools, "add_note", arxiv_id="2608.01101", content="x", reason="因为要复核")
    events = container.repo.events_since(since_seq=0, actor="ai", op="add_note")
    assert "因为要复核" in [e["reason"] for e in events["events"]]
    # mecha History 审计侧同一 reason（两份 journal 的 reason 维度互引不断裂）
    audits = [e for e in stack["history"].events() if e.key == "command.add_note"]
    assert audits and audits[-1].reason == "因为要复核"


def test_gate_config_override_does_not_mutate_shared_settings(tmp_path):
    """复审②：gate 配置经**线程局部覆盖**作用于 run，**不改共享 settings**（并发安全）。

    能红：旧的 mutate-restore 会在运行窗口内改 container.settings（并发的人类路径
    误读 AI 配置）；现改为线程局部覆盖，共享 settings 始终不变。
    """
    container, stack = _stack(tmp_path)
    tools = stack["tools"]
    original_max = container.settings.scoring.max_papers
    _call(tools, "set_config", key="scoring.max_papers", value=1)
    assert stack["gate"].snapshot["scoring.max_papers"] == 1
    out = _call(tools, "run_pipeline", force=True)
    assert out["ok"] and out["selected"] <= 1          # gate 配置对本次 run 生效
    # 共享 settings 未被就地改：并发的 Web/CLI 路径不会误读到 gate 值
    assert container.settings.scoring.max_papers == original_max


def test_undo_result_ref_cross_references_undone_seq(tmp_path):
    """复审③：undo 命令的 result_ref 带 ``undone_seq``（最该被追溯的撤销不断互引）。

    能红：_REF_KEYS 写成 ``undid_seq`` 时 result_ref=None，此断言失败。
    """
    _container, stack = _stack(tmp_path)
    tools = stack["tools"]
    _call(tools, "add_note", arxiv_id="2608.01101", content="待撤销")
    out = _call(tools, "undo_change", seq=0)
    assert out["ok"]
    audits = [e for e in stack["history"].events() if e.key == "command.undo_change"]
    assert audits, "undo 应落一条命令审计"
    ref = audits[-1].value["result_ref"]
    assert ref is not None and ref.get("undone_seq") is not None


# ============================================= 从旧 test_journal/test_mcp_* 迁移的覆盖
def test_write_attributed_to_ai_and_undoable(tmp_path):
    """（迁自 test_journal）写入归因 actor=ai + reason 落域 journal；undo 可撤销。"""
    container, stack = _stack(tmp_path)
    tools = stack["tools"]
    out = _call(tools, "star_paper", arxiv_id="2608.01101", reason="AI 觉得值得收藏")
    assert out["ok"] and out["star"] is True
    events = container.repo.events_since(since_seq=0, actor="ai", op="star_paper")
    assert events["events"] and events["events"][0]["reason"] == "AI 觉得值得收藏"
    undone = _call(tools, "undo_change", seq=0, reason="撤销收藏")
    assert undone["ok"] and undone["op"] == "star_paper"
    detail = _call(tools, "read_paper", arxiv_id="2608.01101")
    assert detail["reading"]["star"] is False


def test_undo_irreversible_is_teachable(tmp_path):
    """（迁自 test_journal）撤销不可逆操作（简报定稿）→ 可教学拒绝。"""
    _container, stack = _stack(tmp_path)
    tools = stack["tools"]
    _call(tools, "run_pipeline", force=True, reason="AI 跑批")
    activity = _call(tools, "read_activity", op="save_briefing", limit=5)
    assert activity["events"], "定稿应落一条 save_briefing 事件"
    seq = activity["events"][0]["seq"]
    out = _call(tools, "undo_change", seq=seq, reason="想撤")
    assert out["ok"] is False and out["error"]["kind"] == "irreversible"
    assert out["error"]["hint"]


def test_read_capability_catch_all_has_hint(tmp_path, monkeypatch):
    """（迁自 test_journal）未分类异常的回程也必须可教学（带 hint）。"""
    container, stack = _stack(tmp_path)

    def boom(_arxiv_id):
        raise ValueError("模拟未分类错误")

    monkeypatch.setattr(container.repo, "get_paper", boom)
    out = _call(stack["tools"], "read_paper", arxiv_id="2608.01101")
    assert out["ok"] is False and out["error"]["kind"] == "ValueError"
    assert out["error"]["hint"], "兜底错误必须带 hint"


def test_boot_writes_leave_domain_traces(tmp_path):
    """⭐ 启动期两次写也**留痕**（本批补）：建表/迁移、把 YAML 主题真相源对齐进 DB。

    它们都是**域状态改动**，从前在框架外悄悄发生、事后无从查起。两条都记**域 journal**
    （`record_op`：`reversible=0`），归因 `actor="system"`（启动不是人、也不是 AI）。

    **对偶**：这两条**不进 mecha History**——History 只记状态变更（`/config`/`/history` 看的是它），
    而"启动做了一次建表/对齐"属于**只读面的使用日志**，只该在 `/activity` 看到。
    """
    container, stack = _stack(tmp_path)
    repo = container.repo
    assert repo.events_since(since_seq=0, actor="system", op="migrate")["count"] >= 1
    assert repo.events_since(since_seq=0, actor="system", op="sync_topics_boot")["count"] >= 1
    hist_keys = {e.key for e in stack["history"].events()}
    assert "migrate" not in hist_keys and "sync_topics_boot" not in hist_keys, (
        "启动留痕属于域 journal（/activity），不该混进 mecha History")
