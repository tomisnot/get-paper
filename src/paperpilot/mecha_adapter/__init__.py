"""PaperPilot 的 mecha 接线层。

分工：
- ``config``   可写标量配置的单一来源（schema + 校验 + 种入 Gate）
- ``scope``    作用域与写权
- ``queries``  只读查询（Surface.QUERY）
- ``commands`` 有副作用的动作（CommandSpec → CommandRegistry）
- ``tools``    AI 能力面：读工具直定义，写工具由命令投影（不手抄第二份）
- ``monitor``  只读监控摘要 + cockpit 端点
- ``stack``    装配点：把上面这些接成一台 Software
- ``hub``      进程入口：起 MCP / 监控 / 控制 / Web 四个面
"""

from __future__ import annotations

from .hub import main as hub_main
from .stack import Stack, boot_mode, build_stack

__all__ = ["Stack", "boot_mode", "build_stack", "hub_main"]
