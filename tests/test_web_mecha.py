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
    """建 container + 共享 mecha 栈 + Web 客户端（stack 传入 → 走门）。

    ⚠ **栈以 `Mode.OPEN` 起步**（2026-09-26 行为变更）：GP 的启动默认从 LOCKED 改成 OPEN
    （"不卡写权"），且 `human_write` **不再抢占**（人写不再自动把模式切成 HUMAN）。
    ⇒ 判据要**显式摆好它假设的模式**，而不是像从前那样"靠人写自动取权"把模式弄对。
    """
    settings = make_settings(tmp_path / "data")
    container = build_container(settings)
    container.repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")))
    stack = build_stack(container, tmp_path, "mecha", mode=Mode.OPEN)
    return TestClient(create_app(container, stack)), container, stack


def test_web_write_goes_through_gate_both_journals(tmp_path):
    """Web 收藏 → 经命令面（human 通道）：域 journal + mecha 审计都记 actor=human。"""
    client, container, stack = _gated(tmp_path)
    resp = client.post("/papers/2608.01101/star", follow_redirects=False)
    assert resp.status_code == 303
    # ⚠ 本批行为变更：人写**不再**自动取写权（`human_write` 的抢占已删）⇒ 模式**不变**（仍 OPEN）
    assert stack["authority"].mode is Mode.OPEN
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
    client.post("/papers/2608.01101/star", follow_redirects=False)   # Web 写（模式不变：仍 OPEN）
    page = client.get("/settings")
    assert page.status_code == 200
    assert "写权模式" in page.text
    assert "open" in page.text                   # 卡片显示**当前模式**（本批：起步 OPEN）
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


def test_ai_mode_denies_human_write_then_open_allows_both(tmp_path, control_token):
    """⭐ **行为变更的正身**（旧判据 `…_human_write_preempts` 按新事实重写）：

    旧事实：Web 写会自动把模式从 AI 抢回 HUMAN（"人优先"）。
    新事实（2026-09-26 用户裁决「不卡写权」+ 框架 `Mode.OPEN`）：
      * **人写不再抢占**——一个随时会被自己改掉的模式不是模式；
      * `AI` 独占时**人类写被拒**（这是模式的字面意思），**拒绝消息要指出出路**（在同一页可切回放开）；
      * 切回 `open` 后**两侧都能写**，且**谁也不改谁的模式**。

    ⚠ 这是**行为变更**，不是把判据改松：旧断言在这里**必然红**（模式不再变），
    我们**换成新事实**并保留同样的严格度（拒绝要有、出路要有、放开后两侧都要能写）。
    """
    client, _container, stack = _gated(tmp_path)
    resp = client.post("/monitor/mode",
                       data={"target": "ai", "token": control_token}, follow_redirects=False)
    assert resp.status_code == 303
    assert stack["authority"].mode is Mode.AI
    # AI 独占：人类写**被拒**，且模式**不被改写**（不抢占）
    denied = client.post("/papers/2608.01101/star", follow_redirects=True)
    assert "被拒" in denied.text or "拒" in denied.text, denied.text[:400]
    assert stack["authority"].mode is Mode.AI, "人写不该再把模式抢回去（抢占已删）"
    # 人类侧在同一页就能切回放开（拒绝 ≠ 卡死）
    assert client.post("/monitor/mode", data={"target": "open", "token": control_token},
                       follow_redirects=False).status_code == 303
    assert stack["authority"].mode is Mode.OPEN
    assert client.post("/papers/2608.01101/star",
                       follow_redirects=False).status_code == 303      # 人又能写了
    assert stack["authority"].mode is Mode.OPEN, "放开之后人写也不该改模式"


def test_open_mode_both_sides_can_write(tmp_path):
    """⭐ **两向**（缺一不可）：OPEN 下 AI 与人类**都能写**，且互不挤掉对方、模式不变。

    AI 侧走**工具面**（`add_note` 经命令面）、人类侧走 **Web**（POST star）——两条路都在同一个栈里。
    """
    from .test_mecha_adapter import _call

    client, container, stack = _gated(tmp_path)          # _gated 已以 OPEN 起步
    assert stack["authority"].mode is Mode.OPEN
    ai_out = _call(stack["tools"], "add_note", arxiv_id="2608.01101", content="AI 的笔记")
    assert ai_out["ok"] is True, ai_out                  # AI 能写
    assert client.post("/papers/2608.01101/star",
                       follow_redirects=False).status_code == 303    # 人类能写
    assert stack["authority"].mode is Mode.OPEN          # 谁也不抢谁
    ai_again = _call(stack["tools"], "mark_read", arxiv_id="2608.01101", read=True)
    assert ai_again["ok"] is True, "人被允许写之后，AI 不该被挤掉"    # AI 还能写


def test_locked_stops_both_sides(tmp_path, control_token):
    """**对偶**：锁定（急停）之后**两侧都被拒**，且域零改动。

    人的 `<button>锁定</button>` 必须仍然好使——放开写权不等于把急停拆了。
    """
    from .test_mecha_adapter import _call

    client, container, stack = _gated(tmp_path)
    assert client.post("/monitor/mode", data={"target": "locked", "token": control_token},
                       follow_redirects=False).status_code == 303
    assert stack["authority"].mode is Mode.LOCKED
    ai_out = _call(stack["tools"], "add_note", arxiv_id="2608.01101", content="锁定后的 AI 写")
    assert ai_out["ok"] is False and ai_out["error"]["kind"] == "authority_locked"
    human_out = client.post("/papers/2608.01101/star", follow_redirects=True)
    assert stack["authority"].mode is Mode.LOCKED
    assert container.repo.events_since(since_seq=0, op="star_paper")["count"] == 0
    assert container.repo.events_since(since_seq=0, op="add_note")["count"] == 0
    assert "拒" in human_out.text


def test_open_mode_keeps_attribution(tmp_path):
    """**归因不模糊**：OPEN 放开的只是**写权**，不是**归因**——
    人类写仍记 `actor=human`、AI 写仍记 `actor=ai`（域 journal 与框架审计两侧都查）。
    """
    from .test_mecha_adapter import _call

    client, container, stack = _gated(tmp_path)
    assert client.post("/papers/2608.01101/star",
                       follow_redirects=False).status_code == 303
    assert _call(stack["tools"], "add_note", arxiv_id="2608.01101",
                 content="AI 写的")["ok"] is True
    # 域 journal：两条各自的 actor
    assert container.repo.events_since(since_seq=0, actor="human", op="star_paper")["count"] >= 1
    assert container.repo.events_since(since_seq=0, actor="ai", op="add_note")["count"] >= 1
    # 框架 History：命令审计的 actor 也各归各的
    audits = [(e.key, e.actor) for e in stack["history"].events()
              if e.key in ("command.star_paper", "command.add_note")]
    assert ("command.star_paper", "human") in audits
    assert ("command.add_note", "ai") in audits


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
    """dsh 面板取 `/history`：模式随栈当前态（本批：起步 OPEN）、事件里看得到 command 审计、带 CORS。"""
    client, _container, _stack = _gated(tmp_path)
    client.post("/papers/2608.01101/star", follow_redirects=False)
    r = client.get("/history?since_seq=0")
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"   # dsh :3081 跨源必需
    body = r.json()
    assert body["ok"] is True and body["mode"] == "open"          # 人写不再改模式（抢占已删）
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


# ------------------------------------------------ 记录仪一键撤销（本批补的控件，原先只有文案）
def test_activity_undo_button_and_gated_undo(tmp_path):
    """⭐ 记录仪**真能撤销**（本批补的控件）：页面有按钮；POST 走**命令面** ⇒ 有审计。

    原先的病：`settings.html` 两处文案承诺"可在 /activity 页 undo"，而**那页没有控件**
    ⇒ 文案承诺做不到的事。本批补上（且**只有可逆事件**才给按钮）。
    """
    client, container, stack = _gated(tmp_path)
    client.post("/papers/2608.01101/star", follow_redirects=False)   # 造一条**可逆**事件
    assert container.repo.events_since(since_seq=0, op="star_paper")["count"] >= 1
    page = client.get("/activity")
    assert page.status_code == 200
    assert 'action="/activity/undo"' in page.text and "撤销" in page.text
    resp = client.post("/activity/undo", data={"seq": 0}, follow_redirects=False)
    assert resp.status_code == 303
    assert container.repo.events_since(since_seq=0, op="undo")["count"] >= 1
    assert any(e.key == "command.undo_change" for e in stack["history"].events())


def test_activity_undo_hidden_without_stack(tmp_path):
    """**对偶**：无栈（独立 Web/单测）时**不渲染**撤销按钮，POST 也如实报"未接监控面"——
    否则又会回到"按钮点了没用"那类假承诺。"""
    client = TestClient(create_app(build_container(make_settings(tmp_path / "data"))))
    page = client.get("/activity")
    assert page.status_code == 200 and 'action="/activity/undo"' not in page.text
    resp = client.post("/activity/undo", data={"seq": 0}, follow_redirects=False)
    assert resp.status_code == 303
    # ⚠ 重定向 Location 里的中文是百分号编码的 ⇒ 先解码再断言（第一版直接 in 判断，假红）
    from urllib.parse import unquote

    assert "未接监控面" in unquote(resp.headers["location"])


# ------------------------------------------------ reset_profile：人类专属（本批补人入口）
def test_reset_profile_human_entry_but_still_not_for_ai(tmp_path):
    """⭐ `reset_profile` 是**人类专属**：本批给人补了入口（走命令面 + 审计），
    **而 AI 工具面里仍然没有它**——"给人入口"没有顺手变成"给 AI 入口"（原设计意图保住）。
    """
    client, _container, stack = _gated(tmp_path)
    page = client.get("/settings")
    assert 'action="/settings/profile/reset"' in page.text
    assert client.post("/settings/profile/reset", data={"kind": ""},
                       follow_redirects=False).status_code == 303
    assert any(e.key == "command.reset_profile" for e in stack["history"].events())
    tool_names = {s["name"] for s in stack["tools"].schemas()}
    assert "reset_profile" not in tool_names, (
        "reset_profile 不该出现在 AI 工具面（它改的是 AI 自己的标尺）")


# ------------------------------------------- 第 5 件：全局参数的"两个真相源"（schema 内 6 键 vs YAML 3 键）
_GENERAL_FORM = {
    "lookback_days": "4", "threshold": "0.5", "quota_per_topic": "3",
    "max_papers": "9", "max_per_author": "2", "must_read_cap": "4",
    "review_floor": "0.3", "webhook_url": "https://example.com/hook",
}


def test_settings_general_gate_keys_go_through_gate(tmp_path):
    """⭐ 第 5 件：`CONFIG_SCHEMA` 内的 6 键经**批量命令**走门 ⇒
    ① 框架账上有 `command.set_config_batch`；② **Gate 快照真的变了**。

    **对偶（本条的严格之处）**：只 YAML 变了**不算过** ⇒ 所以断言的是**快照**，
    不是 `settings.yaml`、也不是内存 `settings`（那两样从前就是被直写的地方）。
    """
    client, _container, stack = _gated(tmp_path)
    resp = client.post("/settings/general", data=_GENERAL_FORM, follow_redirects=False)
    assert resp.status_code == 303
    assert any(e.key == "command.set_config_batch" for e in stack["history"].events()), (
        "框架账上没有 command.set_config_batch ⇒ 这 6 个键没走门（又回到两个真相源）")
    snap = stack["gate"].snapshot
    assert snap["scoring.max_papers"] == 9 and snap["lookback_days"] == 4, (
        "Gate 快照没变 ⇒ 权威没被改到；只 YAML 变了不算过")


def test_settings_general_batch_is_atomic(tmp_path):
    """⭐ **批量半成功**：一个键非法（阈值越界 5.0，schema 是 0..1）⇒ **一个都不落地**。

    这正是 `gate.set_batch` 的原子语义（"先全校验、再全落地"）；也验本路由在整批被拒时
    **不写 YAML**（别留半拉子）。
    """
    client, container, stack = _gated(tmp_path)
    before = dict(stack["gate"].snapshot)
    resp = client.post("/settings/general", data=dict(_GENERAL_FORM, threshold="5.0"),
                       follow_redirects=True)
    assert stack["gate"].snapshot == before, "整批应一个都不落地（快照一个字节不动）"
    assert container.settings.scoring.max_papers != 9, (
        "整批被拒时连内存/YAML 都不该被改（不留半拉子）")
    assert "拒" in resp.text or "bad_value" in resp.text     # 失败要可读地回给页面


def test_settings_general_yaml_keys_are_traced_and_declared(tmp_path):
    """3 个**不在** schema 的键（`review_floor` / `notify.*`）⇒ 裁决 A2：
    **归 YAML**（面板看不见它们是对的）+ **改动记一条域事件**（有痕可查）
    + **UI 明说这件事**（别让人以为它们在门里）。
    """
    client, container, _stack = _gated(tmp_path)
    client.post("/settings/general", data=dict(_GENERAL_FORM, webhook_url="https://x/y"),
                follow_redirects=False)
    assert container.repo.events_since(since_seq=0, op="set_settings")["count"] >= 1
    page = client.get("/settings").text
    assert "归 YAML" in page and "不在 Gate 内" in page, "UI 必须明说这 3 键不归门管"
