"""装配点：把领域对象接成一台 mecha 软件。

顺序即契约（开发者指南 §11）：assemble → 种配置 → 装回进程 → 注册查询 →
注册命令 → 绑作用域 → 投影工具。任何一步都不许跳过，因为后一步的输入是前一步的产物。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from mecha.assembly import Software, assemble
from mecha.authority import Mode
from mecha.surface import ExecutionContext

from ..config import Settings
from ..context import Context
from . import commands as commands_mod
from . import config as config_mod
from . import monitor as monitor_mod
from . import queries as queries_mod
from . import scope as scope_mod
from . import tools as tools_mod

#: 框架记账落在**项目自己**的数据目录里（`<root>/.mecha/journal.jsonl` 只作兜底）。
DATA_DIR_NAME = ".mecha"


@dataclass
class Stack:
    software: Software
    ctx: Context
    tools: object
    command_names: list[str]
    query_names: list[str]
    root: Path
    data_dir_name: str

    # ---- 便捷取用（测试与 Web 走同一个入口，不摸内部）----
    @property
    def gate(self):
        return self.software.gate

    @property
    def history(self):
        return self.software.history

    @property
    def authority(self):
        return self.software.authority

    @property
    def commands(self):
        return self.software.commands

    @property
    def channels(self):
        return self.software.channels

    @property
    def settings(self) -> Settings:
        return self.ctx.settings

    @property
    def repo(self):
        return self.ctx.repo

    @property
    def mode(self) -> str:
        return str(getattr(self.authority.mode, "value", self.authority.mode))

    def invoke(self, name: str, args: dict, *, side: str = "ai",
               approval=None) -> dict:
        """走**同一条门**调用命令：Web（human）与 AI（ai）只是通道不同。"""
        channel = self.channels.get(side) or self.channels["ai"]
        return self.commands.invoke(
            name, dict(args), context=ExecutionContext(), gate=self.gate,
            channel=channel, approval=approval or self.software.approval)

    def invoke_human(self, name: str, **args) -> dict:
        return self.invoke(name, args, side="human")

    def close(self) -> None:
        self.software.close()


def build_stack(ctx: Context, *, root: str | Path | None = None,
                data_dir_name: str = DATA_DIR_NAME,
                mode: Mode | None = None) -> Stack:
    """真实入口与测试共用这一个装配函数。"""
    base = Path(root) if root is not None else Path(
        os.environ.get("MECHA_SOFTWARE_ROOT") or Path.cwd())
    software = assemble(
        root=base,
        data_dir_name=data_dir_name,
        summarizer=monitor_mod.summarize,
        validate=config_mod.validate,
        project="paperpilot",
        mode=mode if mode is not None else boot_mode(),
        # 账住在状态旁边：配置写 / 域写 / 命令审计共用同一张 events 表、同一个序列。
        log_sink=ctx.repo.events_log,
    )
    config_mod.seed(software.gate, ctx.settings)
    config_mod.apply_snapshot(ctx.settings, software.gate.snapshot)
    query_names = queries_mod.build_queries(ctx, software)
    command_names = commands_mod.build_commands(ctx, software)
    policy = scope_mod.build_scope_policy()
    tools = tools_mod.build_tools(ctx, software, policy=policy, side="ai")
    return Stack(software=software, ctx=ctx, tools=tools,
                 command_names=command_names, query_names=query_names,
                 root=base, data_dir_name=data_dir_name)


#: 出厂写权：**人与 AI 共用**（这个软件的设计就是如此）。
#: 框架出厂是 LOCKED（安全默认），"从哪起步"归项目声明。
BOOT_MODE_DEFAULT = "open"
_BOOT_MODES = {"open": Mode.OPEN, "locked": Mode.LOCKED,
               "human": Mode.HUMAN, "ai": Mode.AI}


def boot_mode(value: str = "") -> Mode:
    """起步写权：显式值 > `MECHA_INITIAL_MODE` > 本仓默认。

    ⚠ 一律 `.value`：`Mode` 是 str 枚举，`str(Mode.AI)` 给的是 `'Mode.AI'`。
    """
    raw = str(value or "").strip().lower() or \
        str(os.environ.get("MECHA_INITIAL_MODE", "")).strip().lower()
    if raw in _BOOT_MODES:
        return _BOOT_MODES[raw]
    return _BOOT_MODES[BOOT_MODE_DEFAULT]
