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
    """GP 的"开窗"语义：**没有进程内窗口** ⇒ 响亮失败，并说清人面在哪。

    ⚠ 这句**必须由项目给**（框架的默认措辞是通用的）：GP 的人面是 **Web 工作台**
    （浏览器里的页面，随服务一直在），没有"进程内开窗"这回事。
    回调抛 `MechaError` 会被框架**原样传播** ⇒ 这条说明不会丢。
    """
    raise MechaError(
        "GP 没有需要打开的「手动面」：它的人面是 Web 工作台"
        "（浏览器里的页面，随服务一直在），不随本进程开关。",
        kind="no_manual_surface",
        hint="人的写入口在 Web 页 `/settings` 的「写权模式」卡；本操作对 GP 恒为响亮失败。")


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
        # 开窗：GP 没有窗口 ⇒ 响亮失败（本项目自己的那句说明）。
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


__all__ = ["HOW_TO_OPEN", "control_registry", "start_control"]
