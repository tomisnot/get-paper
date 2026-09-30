"""Phase 3 · cockpit 只读监控端点验收。

对齐接入指南第 4 步 + 交接文档 §7 Phase 3：四路由**逐字段**、概括能被原始**证伪**
（disputed）、只读红线（POST 恒 404）、数据真到了（R8：空表 vs 空表不算通过）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from mecha.monitor import Claim, Monitor

from paperpilot.mecha_adapter.monitor import paperpilot_summarizer, start_cockpit

from .test_mecha_adapter import _call, _stack


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - 回环端点
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _post(url: str):
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_cockpit_four_routes(tmp_path):
    """四核心路由 + /summary 逐字段；POST/未知路由 404。"""
    _container, stack = _stack(tmp_path)          # open_ai=True
    tools = stack["tools"]
    _call(tools, "add_note", arxiv_id="2608.01101", content="cockpit 测")
    _call(tools, "set_config", key="scoring.max_papers", value=3)

    ep = start_cockpit(stack, port=0)
    try:
        base = ep.url
        # /status：{ok, mode}
        st, status = _get(base + "/status")
        assert st == 200 and status["ok"] is True and status["mode"] == "ai"

        # /activity：轻量 6 键，且**数据真到了**（含命令审计 + 配置写）
        st, act = _get(base + "/activity")
        assert st == 200 and act["ok"] is True and act["mode"] == "ai"
        assert act["events"], "空表 vs 空表不算通过：必须自证事件真落史（R8）"
        e0 = act["events"][0]
        assert set(e0) == {"kind", "actor", "target", "value", "seq", "call_id"}
        # ⚠ mecha 2026-09-30 事件改名（key→target / value→after）：命令审计的 `target` 现在是**空**，
        # 命令名落在 `value["command"]` ⇒ 判据按**新语义**核对"审计 + 配置写都到了"。
        audit_cmds = {e["value"].get("command") for e in act["events"]
                      if isinstance(e["value"], dict)}
        targets = {e["target"] for e in act["events"]}
        assert "add_note" in audit_cmds and "scoring.max_papers" in targets

        # /history：飞行记录 9 键（新形状：target/after/before/reason/call_id/ts）
        st, hist = _get(base + "/history")
        assert st == 200 and hist["ok"] is True
        h0 = hist["events"][0]
        assert set(h0) == {"seq", "kind", "actor", "target", "after",
                           "before", "reason", "call_id", "ts"}

        # /config：schema 树（6 个**域**配置键）。
        # ⚠ 框架 2026-09-26 起：**审计键不再进域快照**（`Gate.record` 只记史、不改快照）⇒
        # `command.*` 既不在 groups（没 schema）**也不在 orphans**（压根不在快照里）。
        # 原先"命令审计键不在 schema → 落 orphans"的期望因此**作废**（框架改好了、期望过时），
        # 换成下面两句**新事实**：框架若回退（审计键又掺进快照）⇒ 立刻红。
        st, cfg = _get(base + "/config")
        assert st == 200 and cfg["ok"] is True and cfg["has_schema"] is True
        assert cfg["n_keys"] == 6                    # 域键数不变（审计键不掺进来）
        assert "scoring" in cfg["groups"] and "fetch" in cfg["groups"]
        orphan_names = {o["name"] for o in cfg["orphans"]}
        assert not [n for n in orphan_names if n.startswith("command.")], (
            f"审计键不该出现在域快照的 orphans 里：{sorted(orphan_names)}")

        # /summary：Claim 对账通过（不存疑）；概括复述原始配置值
        st, summ = _get(base + "/summary")
        assert st == 200 and summ["ok"] is True and summ["disputed"] is False
        assert summ["summary"]["write_count"] >= 1
        assert summ["summary"]["max_papers"] == {"value": 3,
                                                 "target": "scoring.max_papers"}

        # 只读红线：POST 恒 404；未知路由 404
        st, body = _post(base + "/status")
        assert st == 404 and body["ok"] is False
        st, body = _get(base + "/nope")
        assert st == 404 and body["ok"] is False and body["error"] == "unknown endpoint"
    finally:
        ep.stop()


def test_summary_can_be_falsified_by_raw(tmp_path):
    """概括能被原始**证伪**（R7 能红证据）：撒谎的 Claim → disputed，原始赢。"""
    _container, stack = _stack(tmp_path)
    _call(stack["tools"], "set_config", key="scoring.max_papers", value=3)
    events = stack["history"].events

    # 撒谎 summarizer：声称 max_papers=999，与原始（3）不符 → 必须被判存疑
    lying = Monitor(events, lambda evs: {"max_papers": Claim(999, target="scoring.max_papers")})
    view = lying.read()
    assert view.disputed is True
    assert "scoring.max_papers" in view.dispute_reason
    assert view.raw_snapshot["scoring.max_papers"] == 3      # 原始赢

    # 声称一个原始史里不存在的键 → 也算存疑
    ghost = Monitor(events, lambda evs: {"x": Claim(1, target="no.such.key")})
    assert ghost.read().disputed is True

    # 真 summarizer 不撒谎 → 不存疑
    real = Monitor(events, paperpilot_summarizer).read()
    assert real.disputed is False


def test_cockpit_empty_history_is_honest_not_fake(tmp_path):
    """无任何写时：/activity 如实空表（ok=True, events=[]），mode 仍真（LOCKED）。"""
    _container, stack = _stack(tmp_path, open_ai=False)
    ep = start_cockpit(stack, port=0)
    try:
        st, status = _get(ep.url + "/status")
        assert status["mode"] == "locked"                 # 出厂 LOCKED 如实透出
        st, act = _get(ep.url + "/activity")
        # 种子配置已落史（seed），故 events 非空且全是 bootstrap 的配置键
        assert act["ok"] is True
        assert all(e["actor"] == "bootstrap" for e in act["events"])
        assert not any(e["target"].startswith("command.") for e in act["events"])
    finally:
        ep.stop()
