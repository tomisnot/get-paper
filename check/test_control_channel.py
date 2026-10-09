"""控制通道（§7.1.1 契约）的判据 —— 人侧专用那条端点。

## ⚠⭐ 口令去掉之后，「AI 不能把自己解锁」靠什么？（这一节是给下一个人的）

**不靠口令**（用户裁决：本机可信、无口令 —— 架构文 §3 裁决 2）。
靠 **「切写权的操作不在 AI 的能力面上」**，而 AI 也**拿不到**控制端点的地址：

| 面 | 端口文件 | 有什么 | 谁连 |
|---|---|---|---|
| AI 能力面 | `.mcp-port` | 61 个工具，其中写权相关**只有** `read_write_authority`（只读自省） | AI / dsh |
| **控制端点** | `.control-port` | `read_write_authority` · **`set_write_authority`** · **`open_manual_surface`** | **只有框架/面板** |

⇒ 判据 ①（下面第一条）钉的正是"**AI 面上没有那两个改操作**"—— 这就是"没口令之后防线在哪"的**可判形式**。
⚠ 若哪天有人"顺手"把它们注册进 AI 那条面，**本文件第一条当场红**。
"""

from __future__ import annotations

from pathlib import Path

from mecha.authority import Mode

from paperpilot.mecha_adapter.control import build_control_registry
from tests.test_mecha_adapter import _call, _stack

REPO = Path(__file__).resolve().parents[1]


def _control_reg(tmp_path):
    """建一个真栈 + 控制面注册表（**同一个 authority** ⇒ 与 AI 面共享同一道门）。"""
    _container, stack = _stack(tmp_path, open_ai=False)
    reg = build_control_registry(stack["authority"], gate=stack["gate"])
    return stack, reg


def test_ai_face_has_no_write_authority_setters(tmp_path):
    """⭐ ① AI 的能力面上**没有** `set_write_authority` / `open_manual_surface`。

    ⚠ **这一条就是"没口令之后的防线"**：AI 若能在自己那条面上切写权，就等于**给自己解锁**
    （旧口令那套存在的唯一理由）。现在防线是**面的存在性**（那两个操作只在 `.control-port`，
    而那个地址只写进文件给框架/面板读）。

    红证（实测）：把 `set_write_authority` 也 `register` 进 AI 那条面（`tools.build_tool_registry`）
    ⇒ 本判据当场红。
    """
    _container, stack = _stack(tmp_path, open_ai=False)
    names = {s["name"] for s in stack["tools"].schemas()}
    assert "read_write_authority" in names          # 只读自省**该在** AI 面上（先查后写）
    assert "set_write_authority" not in names, "AI 面上出现了切写权 —— 它能给自己解锁了"
    assert "open_manual_surface" not in names, "AI 面上出现了开手动面"


def test_control_surface_has_the_three_operations(tmp_path):
    """② 控制面三个操作齐（**读**用框架唯一实现；set/open 只在这里）。"""
    _stack_, reg = _control_reg(tmp_path)
    names = {s["name"] for s in reg.schemas()}
    assert names == {"read_write_authority", "set_write_authority", "open_manual_surface"}, names


def test_set_write_authority_takes_effect_immediately(tmp_path):
    """⭐ ③ **切完立刻生效**：切 `human` ⇒ AI 侧**下一次写当场被拒**（不需要重连/重装配）。

    红证（实测）：把 `_set_write_authority` 改成"只记下来、下次装配再应用"（例如只 return
    一个待办而不调 `switch_mode`）⇒ 下面第一句 `ai_can_write` 立刻红。
    ⚠ 反向也对（切回 `ai` ⇒ 人侧被拒）——**两向都断言**，否则"永远切到 human"也能绿。
    """
    stack, reg = _control_reg(tmp_path)
    authority, tools = stack["authority"], stack["tools"]

    out = _call(reg, "set_write_authority", mode="human")
    assert out["ok"] is True and out["mode"] == "human"
    assert authority.mode is Mode.HUMAN                    # 立刻生效（同一对象、同一进程）
    denied = _call(tools, "add_note", arxiv_id="2608.01101", content="切 human 后 AI 写")
    assert denied["ok"] is False and denied["error"]["kind"] in (
        "authority_mode_mismatch", "authority_locked"), denied

    out2 = _call(reg, "set_write_authority", mode="ai")
    assert out2["mode"] == "ai" and authority.mode is Mode.AI
    human_denied = _call(tools, "add_note", arxiv_id="2608.01101", content="切 ai 后 AI 写")
    assert human_denied["ok"] is True, "切回 ai 后 AI 应当能写"     # 对偶：不是"永远拒绝"


def test_read_write_authority_four_modes(tmp_path):
    """⭐ ④ `read_write_authority` **四档逐档对**（`ai_can_write`/`human_holds`）。

    ⚠ 为什么必须逐档：只断言"返回了 dict"（或"有这个键"）⇒ 把 `ai_can_write` 恒写 True 也能绿
    —— 那是 R11（观测量必须能变）。红证（实测）：把 `authority_view` 的探侧退化成恒放行
    ⇒ 该档期望 `(False, False)` 实得 `(True, True)` ⇒ 本判据红。
    """
    stack, reg = _control_reg(tmp_path)
    cases = {Mode.LOCKED: (False, False), Mode.AI: (True, False),
             Mode.HUMAN: (False, True), Mode.OPEN: (True, True)}
    seen: list[str] = []
    for mode, (ai_ok, human_ok) in cases.items():
        stack["authority"].switch_mode(mode, side="human")
        v = _call(reg, "read_write_authority")
        assert v["mode"] == mode.value, f"{mode} ⇒ {v['mode']!r}"       # ⚠ 一律 .value
        assert v["ai_can_write"] is ai_ok, f"{mode}: ai_can_write={v['ai_can_write']}"
        assert v["human_holds"] is human_ok, f"{mode}: human_holds={v['human_holds']}"
        assert v["how_to_open"], "how_to_open 空 ⇒ 被拒的人/AI 不知道去哪儿开"
        seen.append(mode.value)
    assert len(set(seen)) == 4                                  # R8：四档真的都走过


def test_read_write_authority_is_pure_query(tmp_path):
    """⭐ ⑤ **纯查询无副作用**：调它前后 mode 不变、账不增长。

    红证（实测）：在 `authority_view` 里加一句 `gate.record(...)` ⇒ 账增长 ⇒ 本判据红。
    """
    stack, reg = _control_reg(tmp_path)
    stack["authority"].switch_mode(Mode.HUMAN, side="human")
    before_events = len(stack["history"].events())
    _call(reg, "read_write_authority")
    _call(reg, "read_write_authority")
    assert stack["authority"].mode is Mode.HUMAN
    assert len(stack["history"].events()) == before_events, "只读自省落了账"


def test_set_write_authority_does_not_self_lock(tmp_path):
    """切到 `ai` 之后**人侧还能把账记下来、也还能切回来**（不走写权门 ⇒ 不会自锁）。

    ⚠ 这是 EL 实测的坑：若 `set_write_authority` 自己也过一次写权门，切到 `ai` 后人侧
    就再也记不了账、也切不回来。
    """
    stack, reg = _control_reg(tmp_path)
    _call(reg, "set_write_authority", mode="ai")
    before = len(stack["history"].events())
    _call(reg, "set_write_authority", mode="locked")            # 人侧仍能切
    assert stack["authority"].mode is Mode.LOCKED
    after = stack["history"].events()
    assert len(after) > before, "切写权没落账（契约要求记同一本账）"
    assert any(e.op == "control.write_authority" and e.actor == "human" for e in after), \
        "账里应有 control.write_authority 且 actor=human"


def test_open_manual_surface_fails_loudly(tmp_path):
    """⭐ ⑥ GP **没有窗口** ⇒ `open_manual_surface` **响亮失败**（不是静默成功）。

    ⚠ 实测的"响亮"形状：工具层回**归一化失败信封** `{ok:false, error:{kind:"no_manual_surface",…}}`
    （经工具面/控制端点给出的 payload 也是它；**不是** MCP 协议级 `isError` —— 那是框架的归一化口径，
    不在这里断言成另一种形状）。
    红证（实测）：把那句 `raise MechaError(...)` 换成 `return {"ok": True}`
    ⇒ 本判据红（它断言的是 `ok is False` + `kind` + 消息说清人面在哪）。
    """
    _stack_, reg = _control_reg(tmp_path)
    out = _call(reg, "open_manual_surface")
    assert out["ok"] is False, f"没窗口却回了成功：{out}"
    assert out["error"]["kind"] == "no_manual_surface", out["error"]
    msg = str(out["error"].get("message") or "") + str(out["error"].get("hint") or "")
    assert "Web" in msg and "settings" in msg, f"失败消息没说清人面在哪：{msg}"


def test_unknown_mode_is_loud(tmp_path):
    """未知 mode ⇒ **响亮失败**（不是静默当默认值）。"""
    stack, reg = _control_reg(tmp_path)
    before = stack["authority"].mode
    out = _call(reg, "set_write_authority", mode="Mode.AI")     # ⚠ 那个经典坑的字符串形状
    assert out["ok"] is False and out["error"]["kind"] == "bad_mode", out
    assert stack["authority"].mode is before, "未知 mode 竟改了写权"


def test_control_port_never_leaks_into_ai_visible_text(tmp_path):
    """⭐ ⑦ 端口号**不进任何给 AI 的文本**（工具说明 / how_to_open / 回执）。

    能红：把 `.control-port` 路径或端口写进 `HOW_TO_OPEN` / 工具 description ⇒ 本判据红。
    """
    from paperpilot.mecha_adapter.control import CONTROL_INSTRUCTIONS, HOW_TO_OPEN

    for text in (CONTROL_INSTRUCTIONS, HOW_TO_OPEN):
        assert "control-port" not in text
        # 不许出现"端口 + 数字"的形状（例如 127.0.0.1:53211 / :53211）
        assert not any(ch.isdigit() for ch in text), f"那段文本里有数字（疑似端口）：{text}"

    # AI 面上那条自省的 how_to_open 同样不许带端口号
    _container, stack = _stack(tmp_path, open_ai=False)
    view = _call(stack["tools"], "read_write_authority")
    assert "control-port" not in view["how_to_open"]
    assert not any(ch.isdigit() for ch in view["how_to_open"]), view["how_to_open"]


def test_no_control_token_anywhere_in_repo():
    """⑦ 口令整套**没了**：仓里（含**被 gitignore 的构建产物**）搜不到口令相关标识。

    ⚠ 这条必须**绕过 git**、并且**扫到 `dsh/lib/*.js` / `*.map`**：
    构建产物是 inlined 的 JS，`git status` 看不见它，python 的 import 也不读它
    ⇒ 只在源码里 grep 会**假绿**（今天已有一处这样的漏检）。
    """
    # ⚠ 口径（2026-10-08 清理归一）：这里禁的是【死标识】（代码/路径/函数名），
    #   **不禁**"口令"这个词本身 —— 换代说明（README / 本判据 / 交接记录）里
    #   解释"口令已去掉、防线改在哪"是**该留的**（口径见 mecha `docs/v2/清理归一-口径.md` §1 C 类）。
    #   ⚠ 若连词都禁，下一个人就查不到"为什么不能走老路"——那正是清理时最容易删错的东西。
    needles = ("control_token", "control-token", "CONTROL_TOKEN",
               "verify_control_token", "publish_control_token")
    roots = [REPO / "src", REPO / "tests", REPO / "check", REPO / "docs",
             REPO / "dsh" / "lib", REPO / ".agents", REPO / "README.md",
             REPO / "mecha_site.py"]
    exts = {".py", ".md", ".ts", ".tsx", ".html", ".js", ".mjs", ".map", ".json", ".yml", ".bat"}
    self_path = Path(__file__).resolve()          # ⚠ 本文件自己列了这些词做针，不扫自己
    hits: list[str] = []
    scanned = 0
    for root in roots:
        files = [root] if root.is_file() else [
            p for p in root.rglob("*")
            if p.is_file() and p.suffix in exts
            and "node_modules" not in p.parts and "__pycache__" not in p.parts
            and p.resolve() != self_path
        ]
        for path in files:
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for needle in needles:
                if needle in text:
                    hits.append(f"{path.relative_to(REPO)}: {needle}")
    assert scanned > 50, f"只扫到 {scanned} 个文件 ⇒ 搜索面不对（假绿）"   # R8 自证
    assert not hits, "口令相关标识还在：\n  " + "\n  ".join(sorted(set(hits))[:20])
