"""GP 的控制端点**接线**（机制在框架 `mecha.software_control`，本项目只给回调与值）。

## 分工（2026-10-08 上提之后）

| 谁 | 给什么 |
|---|---|
| **框架** | `build_control_registry`（三操作的工具表：读用唯一实现、未知 mode 响亮失败、开窗的失败形状）+ `start_control_endpoint`（起端点、**端口回读**、写端口文件）+ 端点说明默认文案 |
| **本项目** | ⭐ **3 个回调**（`switch` 切谁、`audit` 记哪本账、`open_surface` 本项目的"开窗"语义）+ **2 个值**（`server_name`、`how_to_open` 文案） |

⇒ ⚠ **本文件里不许再有第二份机制实现**：`build_control_registry` / `start_control_endpoint`
的定义只在框架里（判据 `check/test_control_channel.py::test_gp_owns_no_control_mechanism` 钉着）。
（历史：GP 与 EL 曾各手写一份 ≈133 行、结构逐行相同的实现 —— 那正是"上提"要消灭的重复。）

## ⚠⭐ 口令去掉之后，「AI 不能把自己解锁」靠什么？

**不靠口令**（用户裁决：本机可信、无口令 —— 架构文 §3 裁决 2）。
靠 **「切写权的操作不在 AI 的能力面上」**，而 AI 也**拿不到**控制端点的地址：

| 面 | 端口文件 | 有什么 | 谁连 |
|---|---|---|---|
| AI 能力面 | `.mcp-port` | 61 个工具，写权相关**只有** `read_write_authority`（只读自省） | AI / dsh |
| **控制端点** | `.control-port` | `read_write_authority` · **`set_write_authority`** · **`open_manual_surface`** | **只有框架/面板** |

⇒ 防线是**面的存在性**（可见性由端点的存在性保证，不靠记得加过滤）。
⚠ 若哪天有人把那两个改操作注册进 AI 那条面，**判据当场红**。

## ⚠ 记账为什么必须走 `Gate.record`（不过写权门）

`set_write_authority` 改的东西**就是"谁能写"**：若它自己也过一次写权门，切到 `ai` 之后
人侧就**再也记不了账、也切不回来**（**自锁**，EL 实测）。⇒ `audit` 回调传的是 `gate.record`
（只记史、不改快照、不过门）—— 账仍是**同一本**。

## ⭐ GP 有**两个**人侧入口，但只有**一段**实现（2026-10-09 补）

| 入口 | 谁用 | 走什么 |
|---|---|---|
| `POST /monitor/mode`（Web 页 `/settings` 的「写权模式」卡） | 人（浏览器） | ⭐ `switch_write_authority(stack, mode)` ⇒ **框架那个操作** |
| `.control-port`（框架/面板） | 框架 | 框架的 `build_control_registry` |

⚠ **原先 Web 那条直调 `authority.switch_mode(...)`** ⇒ 同一个人侧动作两条路，
而**Web 那条不落账**（§4.3：查不到"谁在什么时候把写权切走了"）。
⇒ 现在两条都经 `write_switch(...)` 取**框架那一份**（判据
`check/test_control_channel.py::test_web_route_switch_is_recorded` 钉着"Web 那条也落账"）。

## ⚠ 地址不许外泄

`.control-port` 只给框架读。本文件与两个调用点**都不打印端口号**
（§7.1.1：不许把控制端口写进任何给 AI 的文本/日志）。
"""

from __future__ import annotations

from mecha.authority import Mode
from mecha.errors import MechaError
from mecha.software_control import build_control_registry, start_control_endpoint

#: ⭐ 由**本项目**给的「怎么切到人工」说明（框架**不写死**它：那是项目知识）。
#: 它出现在 `read_write_authority()` 的回执里，给**被拒的一方**（AI 或人）指路。
#: ⚠ **别写端口/凭据**（它会给 AI 读）。
HOW_TO_OPEN = (
    "在 Get Paper 的 Web 页 `/settings` 的「写权模式」卡上切"
    "（本机可信、无口令；也可用 `paperpilot serve --mode <locked|human|ai|open>` 起步）。"
    "⚠ AI 侧不能自解锁：它拿不到控制端点的地址。"
)


def _open_manual_surface() -> str:
    """GP 的"手动面"语义：**它一直开着**（Web 工作台随服务在）⇒ **幂等成功**。

    ⚠ **这不是"没有手动面"** —— 那种情形才该响亮失败（框架的 `no_manual_surface`）。
    GP 的人面是**浏览器里的页面**，随本服务一直在；它不像 EL/ML 的桌面窗口那样需要"开"
    ⇒ 本操作对它**恒为幂等**：回 `already-open`。
    ⚠ **以前这里 `raise MechaError('GP 没有需要打开的「手动面」…')`** —— 那是把
    "**常开**"报成"**失败**"：面板点「切到手动」会弹红字
    "写权已交给人工，但**窗口没开成**"，而人面**早就在那儿**
    （假故障 —— 本工程最忌的"看着像坏了"）。⇒ 已按设计 §7.1.1 补的
    "`open_manual_surface` 的三种结局"改准。
    """
    return "already-open"


#: 「切写权」那个操作的名字 —— ⚠ **取框架的工具名，不在这里另起一个**。
#: （它由 `mecha.software_control.build_control_registry` 注册；改框架那一处就得跟着改。）
WRITE_AUTHORITY_TOOL = "set_write_authority"


def write_switch(authority, *, gate=None):
    """把「切写权」这一个操作**用框架那份实现**取出来：``call(mode) -> {"ok","mode"}``。

    ## 为什么必须有它（实测：GP 有**两个**人侧入口，而它们原先不是同一段实现）

    | 入口 | 谁用 | 原先 |
    |---|---|---|
    | `POST /monitor/mode`（Web 卡） | 人（浏览器） | ⛔ **直调 `authority.switch_mode(...)`** ⇒ **不落账** |
    | `.control-port`（框架的件） | 框架 / 面板 | ✅ 框架那套（含 `gate.record`） |

    ⇒ 同一个人侧动作、两条路，其中一条**在账里查不到** —— 正是 §4.3 要消灭的形态
    （"谁在什么时候把写权切走了"必须可查）。
    ⭐ 这里**不新造机制**：只是把"**取框架注册表里那一个工具**"收敛成一处，
    两个入口都调它 ⇒ `side="human"` 判定、未知 mode 的响亮失败、记账**天然同一份**。
    """
    reg = control_registry(authority, gate=gate)

    def call(mode: str) -> dict:
        out = reg.execute(WRITE_AUTHORITY_TOOL, {"mode": mode})
        if out.get("is_error"):
            failure = out.get("error") or {}
            info = failure.get("info") or {}
            #: ⚠ 把框架的**归一化失败信封**折回 `MechaError`：调用方（Web 层/判据）
            #: 只需认一种失败形状，不必各自解析 `{message, info:{kind,hint}}`。
            raise MechaError(
                str(failure.get("message") or "切写权失败"),
                kind=str(info.get("kind") or "control_failed"),
                hint=str(info.get("hint") or ""),
            )
        return dict(out.get("value") or {})

    return call


def switch_write_authority(stack, mode: str) -> str:
    """**人侧切写权**（Web 卡片那条路）—— ⭐ 与 `.control-port` **同一段框架实现**。

    入参收 `stack`（`{"authority", "gate", …}`）是刻意的：调用方是 **Web 层**，
    而"栈里哪两个键"是**栈自己的形状**（`build_stack` 定的）——
    让 UI 层去拆 mecha 的内部键 = 把实现形状漏进页面代码。

    @returns 给页面显示的**人话**（成功与失败都返回一句；失败**不抛**、也不静默）。
    """
    try:
        out = write_switch(stack["authority"], gate=stack.get("gate"))(
            str(mode).strip().lower())
    except MechaError as exc:
        return f"切换失败：{exc.kind}：{exc.message}"
    return f"写权已切到 {out.get('mode')}"


def control_registry(authority, *, gate=None):
    """建控制面工具表：**框架的件 + 本项目的三个回调**（判据直接用这个，不必起端点）。"""
    return build_control_registry(
        authority,
        # 切写权：一律枚举 + `side="human"`（控制端点只有人侧能连）。
        switch=lambda value: authority.switch_mode(Mode(value), side="human"),
        # 记账：只记史、不过写权门（防自锁）；`gate=None` ⇒ 不记账（判据可省）。
        audit=(None if gate is None else
               (lambda op, val, why: gate.record(op, "authority", val,
                                                 actor="human", reason=why))),
        # 开窗：GP 的人面（Web 工作台）**一直开着** ⇒ 本操作幂等成功（`already-open`）。
        # ⚠ **不是** `no_manual_surface`：那是"真的没有人面"才该走的那一档（见 `_open_manual_surface`）。
        open_surface=_open_manual_surface,
        # "怎么切到人工"是项目知识 ⇒ 由 GP 给。
        how_to_open=HOW_TO_OPEN,
    )


def start_control(authority, *, gate=None, port_file, host: str = "127.0.0.1", log=None):
    """起控制端点：**框架的件 + 本项目的三个回调**。返回 `(endpoint, port)`。

    ⚠ `gate.record`（不是 `Gate.set`）：见模块 docstring 的"自锁"。
    ⚠ `log=None`（默认）：端口号不许进任何日志；起不来由框架抛（响亮）。
    """
    return start_control_endpoint(control_registry(authority, gate=gate),
                                  port_file=port_file,
                                  server_name="paperpilot-control", host=host, log=log)


__all__ = ["HOW_TO_OPEN", "WRITE_AUTHORITY_TOOL", "control_registry", "start_control",
           "switch_write_authority", "write_switch"]
