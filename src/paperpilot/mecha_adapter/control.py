"""控制通道（GP 侧）：第二条 MCP 端点 —— **人侧专用**（三层架构 §7.1.1 契约）。

## 契约（照 `docs/v2/软件-交互-写权-三层架构.md` §7.1.1 实现，**两端别各写一套**）

| 项 | 约定 |
|---|---|
| 端口文件 | ``<项目根>/.control-port``（与 `.mcp-port` **并列、同名族**） |
| 传输 | MCP over streamable-http —— ⭐ **复用框架 `McpEndpoint`**（经 `hub.McpHost`），不发明第二种协议 |
| 谁连 | **只有框架 / 面板** ⇒ ⚠ **不导出给 AI**（AI 拿不到这个地址） |
| 操作 | `read_write_authority()` · `set_write_authority(mode)` · `open_manual_surface()` |
| 入参 | `mode` 是**裸值** `locked`/`human`/`ai`/`open`（⚠ `.value`，不是 `str(Mode)`） |
| 记账 | ⭐ 与 AI 的写**记同一本账**（用 `Gate.record`：只记史、不改快照） |
| 失败 | 未知 mode / 没有手动面 ⇒ **响亮失败**（不许静默） |

## ⚠⭐ 口令去掉之后，「AI 不能把自己解锁」靠什么？

**靠"控制端点的地址不导出给 AI"**（§7.1.1 的**地址传递**保证）：AI 的能力面是**另一条端点**
（`.mcp-port`），那条面上**没有** `set_write_authority` / `open_manual_surface`（判据 5 钉着），
而 `.control-port` 只写进那个文件、**只被框架/面板读** ⇒ AI 既**看不见**那两个操作、也**拿不到地址**去直连。
⇒ ⚠ **"没口令 ≠ 没防线"**：防线是**面的存在性**（可见性由端点的存在性保证，不靠记得加过滤）。
（本机可信、无口令是**用户裁决**：§3 裁决 2 —— 不做防护机制，靠本机可信。）

## ⚠ 为什么 `set_write_authority` 不走写权门

它改的东西**就是"谁能写"**：若它自己也过一次写权门，切到 `ai` 之后人侧就**再也记不了账、也切不回来**
（**自锁**，EL 实测）。⇒ 用 `Gate.record`（只记史、不改快照、不过门）——账仍是**同一本**。

## ⚠ 地址不许外泄

`.control-port` 只给框架读。本文件里**任何日志/工具回执都不打印端口号**
（§7.1.1 明写：不许把控制端口写进任何给 AI 的文本/工具输出）。
"""

from __future__ import annotations

from mecha.authority import Mode, register_authority_read
from mecha.errors import MechaError
from mecha.tools import ToolRegistry, define_tool

#: 控制端点的端口文件（契约 §7.1.1：与 `.mcp-port` 并列、同名族，写在项目根）。
CONTROL_PORT_FILE = ".control-port"

#: 端点服务名（⚠ 只是服务名，**不是**给 AI 的工具前缀——这条端点不导出给 AI）。
CONTROL_SERVER_NAME = "paperpilot-control"

#: ⭐ 由**本项目**给的「怎么切到人工」说明（框架**不写死**它：那是项目知识）。
#: 它出现在 `read_write_authority()` 的回执里，给**被拒的一方**（AI 或人）指路。
HOW_TO_OPEN = (
    "在 Get Paper 的 Web 页 `/settings` 的「写权模式」卡上切"
    "（本机可信、无口令；也可用 `paperpilot serve --mode <locked|human|ai|open>` 起步）。"
    "⚠ AI 侧不能自解锁：它拿不到控制端点的地址。"
)

#: 端点说明（框架会校验体积；⚠ **不写地址、不写端口**）。
CONTROL_INSTRUCTIONS = (
    "本端点供**人侧**（框架 / 面板）控制这份软件：读当前写权、切写权、要求开手动面。"
    "它不是给模型用的能力面——能力面在另一条端点上，两者共用同一套门与同一本账。"
)


def build_control_registry(authority, *, gate=None) -> ToolRegistry:
    """建控制面的工具表（**三个**操作）。

    ⚠ 只读那个用**框架的唯一实现**（`register_authority_read` ⇒ 靠 `gate(side)` 纯查询探侧，
    报的就是闸门真会怎么判）——GP 自写的那份已按用户裁决删掉，**别在这里再写第二份判定规则**。
    """
    reg = ToolRegistry()
    register_authority_read(reg, authority, how_to_open=HOW_TO_OPEN)

    def _set_write_authority(mode):
        raw = str(mode).strip().lower()
        try:
            # ⚠ 一律 `.value`：`Mode` 是 **str-Enum**（`str(Mode.AI)` 给 'Mode.AI' 不是 'ai'）。
            target = Mode(raw)
        except ValueError:
            raise MechaError(
                f"未知写权模式：{mode!r}", kind="bad_mode",
                hint="裸值只认 " + "/".join(m.value for m in Mode)
                     + "（⚠ 不是 `str(Mode.AI)` 那种 'Mode.AI' 形状）")
        # `side="human"`：这一路**只有人侧**能走（控制端点不导出给 AI）。
        authority.switch_mode(target, side="human")
        if gate is not None:
            # ⚠ `Gate.record`（**只记史、不改快照、不过写权门**）：见模块 docstring 的"自锁"。
            gate.record("control.write_authority", "authority",
                        target.value, actor="human", reason="人侧切写权（控制端点）")
        return {"ok": True, "mode": target.value}

    def _open_manual_surface():
        # ⚠ GP 是**人面在 Web 里的人机共享工作台**：**没有"进程内开窗"这回事** ⇒
        # **响亮失败**（不是静默成功，也不假装开了什么）。
        raise MechaError(
            "GP 没有需要打开的「手动面」：它的人面是 Web 工作台"
            "（浏览器里的页面，随服务一直在），不随本进程开关。",
            kind="no_manual_surface",
            hint="人的写入口在 Web 页 `/settings` 的「写权模式」卡；本操作对 GP 恒为响亮失败。")

    reg.register(define_tool(
        name="set_write_authority",
        description=("切换这份软件的写权归属（人侧专用）。切完**立刻生效**："
                     "下一次写权判定就用新值（不允许'等一拍'）。"),
        parameters={"mode": {"type": "string",
                             "description": "裸值：locked / human / ai / open"}},
        output_schema={"type": "object", "required": ["ok", "mode"]},
        execute=lambda mode: _set_write_authority(mode),
    ))
    reg.register(define_tool(
        name="open_manual_surface",
        description=("要求本软件打开「手动面」。⚠ GP 没有可开的窗口（人面是 Web 页）⇒ "
                     "本操作**响亮失败**并说明人面在哪儿。"),
        parameters={},
        output_schema={"type": "object", "required": ["ok"]},
        execute=lambda: _open_manual_surface(),
    ))
    return reg


def start_control_endpoint(registry, *, port_file, host: str = "127.0.0.1", log=None):
    """起控制端点（复用 `hub.McpHost` 那层壳 ⇒ 框架 `McpEndpoint`；**端口回读**）。

    返回**已 start** 的 host（含 `.port`/`.url`）。⚠ 端口号**不要**打印/回显给 AI（§7.1.1）：
    它只写进 `port_file` 给框架读。
    """
    from .hub import McpHost

    host_ = McpHost(registry, server_name=CONTROL_SERVER_NAME,
                    instructions=CONTROL_INSTRUCTIONS, host=host, port=0,
                    port_file=port_file, log=log)
    host_.start()
    return host_


__all__ = ["CONTROL_PORT_FILE", "CONTROL_SERVER_NAME", "CONTROL_INSTRUCTIONS",
           "HOW_TO_OPEN", "build_control_registry", "start_control_endpoint"]
