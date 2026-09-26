"""PaperPilot 的 mecha v2 适配层（Scheme E：commands-centric hybrid）。

把中性的 ``capabilities/`` 22 能力投影成 mecha 的 Engine / ToolRegistry / MCP
端点——**领域实现单一来源仍是 capabilities/**，本层只做薄封装，不复制业务逻辑
（docs/PRINCIPLES.md 信条 9；.qoder/plans 交接文档 §6）。

三份文件的分工（仿 EL ``mecha_v2/``）：
- ``engine``：``PaperPilotEngine``（4 必实现）+ ``make_validator``（Gate 校验）。
- ``tools``：``build_tool_registry``（22 能力 + set/read_config → define_tool）+ 必填项注入来源。
- ``commands``：``build_commands``（13 写能力 → define_command，Phase 2 写治理）。
- ``monitor``：``paperpilot_summarizer``（Claim 对账）+ ``start_cockpit``（只读监控端点，Phase 3）。
- ``hub``：``build_stack``（经 assemble 具名装配点）+ MCP 端点壳。
"""

from __future__ import annotations

from .commands import build_commands
from .engine import PaperPilotEngine, make_validator
from .hub import INSTRUCTIONS, MCP_SERVER_NAME, McpHost, build_mcp_server, build_stack
from .monitor import make_cockpit, paperpilot_summarizer, start_cockpit
from .tools import build_tool_registry

__all__ = [
    "INSTRUCTIONS",
    "MCP_SERVER_NAME",
    "McpHost",
    "PaperPilotEngine",
    "build_commands",
    "build_mcp_server",
    "build_stack",
    "build_tool_registry",
    "make_cockpit",
    "make_validator",
    "paperpilot_summarizer",
    "start_cockpit",
]
