"""Web=human 侧写权接线测试（统一启动 stack 传入时）。

验证：Web 的论文库写经 mecha 命令面（human 通道 + 写权门 + 双 journal 审计）、
**`/settings` 的写权模式卡**（2026-09-26 从 `/monitor` 页搬来）、写权模式切换（人类侧开闸，
**口令 + fail-closed**）。stack=None 的回退路径（直调 retrieval）由 test_web.py 覆盖。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mecha.authority import Mode

from paperpilot.app import control_token as ct
from paperpilot.app.container import build_container
from paperpilot.app.web import create_app
from paperpilot.infra.arxiv import parse_atom
from paperpilot.mecha_adapter.hub import build_stack

from .conftest import SAMPLE_XML, make_settings


@pytest.fixture
def control_token(tmp_path, monkeypatch):
    """把控制口令指到 tmp 并发布一个（判据**不碰**真实家目录）；返回口令字符串。"""
    target = tmp_path / ct.CONTROL_TOKEN_FILE
    token = ct.publish_control_token(target)
    monkeypatch.setattr(ct, "control_token_path", lambda: target)
    return token


def _gated(tmp_path):
    """建 container + 共享 mecha 栈 + Web 客户端（stack 传入 → 走门）。"""
    settings = make_settings(tmp_path / "data")
    container = build_container(settings)
    container.repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")))
    stack = build_stack(container, tmp_path, "mecha")
    return TestClient(create_app(container, stack)), container, stack


def test_web_write_goes_through_gate_both_journals(tmp_path):
    """Web 收藏 → 经命令面（human 通道）：域 journal + mecha 审计都记 actor=human。"""
    client, container, stack = _gated(tmp_path)
    resp = client.post("/papers/2608.01101/star", follow_redirects=False)
    assert resp.status_code == 303
    # 人类面写入自动取写权（LOCKED → HUMAN）
    assert stack["authority"].mode is Mode.HUMAN
    # 域 journal：repo.events 记 actor=human 的 star_paper
    events = container.repo.events_since(since_seq=0, actor="human", op="star_paper")
    assert events["count"] >= 1
    # mecha History：command.star_paper 审计，actor=human（人机同路、同一审计面）
    audits = [e for e in stack["history"].events() if e.key == "command.star_paper"]
    assert audits and audits[-1].actor == "human"


def test_web_add_note_gated(tmp_path):
    """Web 加笔记经门；笔记真落库（回执 303 + 详情页可见）。"""
    client, container, _stack = _gated(tmp_path)
    resp = client.post("/papers/2608.01101/note", data={"content": "门控笔记"},
                       follow_redirects=False)
    assert resp.status_code == 303
    detail = client.get("/papers/2608.01101")
    assert "门控笔记" in detail.text
    assert container.repo.events_since(since_seq=0, actor="human", op="add_note")["count"] >= 1


def test_settings_shows_write_mode_card(tmp_path):
    """写权模式卡在 `/settings` 上（原 `/monitor` 页退役后搬到这里）：显示当前模式 + 口令框。

    ⚠ 原 `test_monitor_page_renders_operator_audit` 测的是"服务端渲染的审计视图"——
    该视图**已退役**（AI 监控改走 dsh 共享资产的原生 tab）⇒ 那条判据随之删除；
    命令审计仍在（mecha History / cockpit 端点 / dsh 面板），只是**不再由 Web 渲染**。
    """
    client, _container, stack = _gated(tmp_path)
    client.post("/papers/2608.01101/star", follow_redirects=False)   # Web 写 ⇒ 自动取 human
    page = client.get("/settings")
    assert page.status_code == 200
    assert "写权模式" in page.text
    assert "human" in page.text
    assert "/monitor/mode" in page.text          # 控制端点路径未动（只换 UI 落点）
    assert "控制口令" in page.text


def test_settings_without_stack_is_honest(tmp_path):
    """无 stack（独立 Web）时，写权卡如实报'未接监控面'，不假装有个可切的模式。"""
    settings = make_settings(tmp_path / "data")
    container = build_container(settings)
    client = TestClient(create_app(container))     # 不传 stack
    page = client.get("/settings")
    assert page.status_code == 200
    assert "未接监控面" in page.text
    assert "/monitor/mode" not in page.text        # 没有可点的控件（不发无意义的请求）


def test_mode_switch_grants_ai_then_human_write_preempts(tmp_path, control_token):
    """人类侧开闸：口令 + 切到 AI 授予写权；随后 Web 写自动取回 human（human 优先）。"""
    client, _container, stack = _gated(tmp_path)
    resp = client.post("/monitor/mode",
                       data={"target": "ai", "token": control_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert stack["authority"].mode is Mode.AI
    # Web 写自动取回 human（单写权：人类在 Web 上动手即取闸）
    client.post("/papers/2608.01101/star", follow_redirects=False)
    assert stack["authority"].mode is Mode.HUMAN


def test_mode_switch_rejects_bad_target(tmp_path, control_token):
    """非法 target 不崩、如实回消息（可教学）。"""
    client, _container, stack = _gated(tmp_path)
    before = stack["authority"].mode
    resp = client.post("/monitor/mode",
                       data={"target": "root", "token": control_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert stack["authority"].mode is before      # 未被非法值改动


# ---------------------------------------------------------------- 控制端点鉴权（fail-closed）

def test_mode_switch_denied_without_token(tmp_path, control_token):
    """⭐ 无口令 ⇒ **403**，且**写权一点没动**（拒绝必须是"什么都没发生"）。"""
    client, _container, stack = _gated(tmp_path)
    before = stack["authority"].mode
    resp = client.post("/monitor/mode", data={"target": "ai"}, follow_redirects=False)
    assert resp.status_code == 403
    assert "写权切换被拒" in resp.text
    assert stack["authority"].mode is before


def test_mode_switch_denied_with_wrong_token(tmp_path, control_token):
    """⭐ 口令不对 ⇒ 403，且**不透露**真口令/不提示"接近了"。"""
    client, _container, stack = _gated(tmp_path)
    before = stack["authority"].mode
    resp = client.post("/monitor/mode",
                       data={"target": "ai", "token": control_token + "x"},
                       follow_redirects=False)
    assert resp.status_code == 403
    assert control_token not in resp.text
    assert stack["authority"].mode is before


def test_mode_switch_fails_closed_when_no_token_published(tmp_path, monkeypatch):
    """⭐ **未配置口令 ⇒ 一律拒绝**：控制端点没有"默认放开"这一档。"""
    missing = tmp_path / "no-such-token"
    monkeypatch.setattr(ct, "control_token_path", lambda: missing)
    client, _container, stack = _gated(tmp_path)
    before = stack["authority"].mode
    for given in ("", "anything", "  "):
        resp = client.post("/monitor/mode",
                           data={"target": "ai", "token": given}, follow_redirects=False)
        assert resp.status_code == 403, f"未发布口令时 token={given!r} 竟放行"
    assert stack["authority"].mode is before
    # 空口令时的文案要**说清是"没配置"**（不是"口令不对"）——否则人会去猜口令
    resp_empty = client.post("/monitor/mode", data={"target": "ai"}, follow_redirects=False)
    assert "未发布控制口令" in resp_empty.text
    # 给了错口令时则说"口令不对"（两种失败**可区分**，与两档错误态同一纪律）
    resp_wrong = client.post("/monitor/mode",
                             data={"target": "ai", "token": "guess"}, follow_redirects=False)
    assert "口令不对" in resp_wrong.text


def test_settings_reports_control_token_state(tmp_path, control_token):
    """有口令时：`/settings` 那张卡**要**口令，并把边界（挡进程不挡 AI）写在明面上。"""
    client, _container, _stack = _gated(tmp_path)
    page = client.get("/settings").text
    assert "控制口令" in page
    assert "挡不住能读你文件的 AI" in page


def test_settings_reports_missing_token(tmp_path, monkeypatch):
    """无口令时：如实说"会被一律拒绝"（fail-closed 让人看得见）。"""
    monkeypatch.setattr(ct, "control_token_path", lambda: tmp_path / "no-such-token")
    client, _container, _stack = _gated(tmp_path)
    assert "未发布控制口令" in client.get("/settings").text


def test_old_monitor_view_is_gone(tmp_path):
    """⭐ `/monitor` 视图**确实退役了**（不是"忘了删"）：404，且导航里没有它的链接。

    这条守的是**删除类改动**的收尾（R14/R13）：删掉的东西**不该还能被访问**，
    也不该在导航/文档里留悬空入口。
    """
    client, _container, _stack = _gated(tmp_path)
    assert client.get("/monitor").status_code == 404
    nav = client.get("/").text
    assert 'href="/monitor"' not in nav
    # 唯一还活着的 /monitor* 是**控制端点**（POST；GET 一律 405/404，不是页面）
    assert client.get("/monitor/mode").status_code in (404, 405)


# ---------------------------------------------------------------- cockpit JSON 路由（dsh 面板取数）
def test_cockpit_history_route_is_cockpit_json_with_cors(tmp_path):
    """dsh 面板取 `/history`：Web 写自动取 human、事件里看得到 command 审计、带 CORS。"""
    client, _container, _stack = _gated(tmp_path)
    client.post("/papers/2608.01101/star", follow_redirects=False)
    r = client.get("/history?since_seq=0")
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"   # dsh :3081 跨源必需
    body = r.json()
    assert body["ok"] is True and body["mode"] == "human"
    assert "command.star_paper" in {e["target"] for e in body["events"]}
    # 9 键契约（含 before/after/reason）——与 mecha cockpit history_records 对齐
    assert {"seq", "kind", "actor", "target", "before", "after", "reason"} <= set(body["events"][0])


def test_cockpit_config_route_shape_and_cors(tmp_path):
    """dsh 面板取 `/config`：has_schema + 6 配置键 + 族树（scoring/fetch），带 CORS。"""
    client, _container, _stack = _gated(tmp_path)
    r = client.get("/config")
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"
    cfg = r.json()
    assert cfg["ok"] is True and cfg["has_schema"] is True
    assert cfg["n_keys"] == 6
    assert "scoring" in cfg["groups"] and "fetch" in cfg["groups"]


def test_cockpit_routes_honest_without_stack(tmp_path):
    """独立 Web（无栈）：/history、/config ⇒ 503 可读错误 + 仍带 CORS，绝不空白/不回落。"""
    client = TestClient(create_app(build_container(make_settings(tmp_path / "data"))))
    for path in ("/history", "/config"):
        r = client.get(path)
        assert r.status_code == 503 and r.json()["ok"] is False
        assert r.headers.get("access-control-allow-origin") == "*"
