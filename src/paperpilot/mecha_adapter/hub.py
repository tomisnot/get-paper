"""mecha v2 MCP 端点（PaperPilot 侧**薄壳**）：投影与传输全在 ``mecha.providers.mcp``。

归属：本文件住 PaperPilot 仓，只保留 **PaperPilot 词汇与策略**——服务名、服务说明、
数据目录/端口文件的默认取值。其余（三步投影、required 解析、null 垫片、失败归一化、
uvicorn 后台线程与端口回读、instructions 体积守卫、端口文件启停清理）**逐条**已是框架
行为（``mecha.providers.mcp``），本文件不再有第二份实现。

⚠ **别传 ``toolhost=``**：``assemble()`` 建出的 ``sw.tools`` 是**空表**（PaperPilot 的工具
注册在 ``build_tool_registry`` 里，从不写进 ``sw.tools``）；把它当 toolhost 传进
端点会让工具「列得出、调不动」（``toolhost_registry_mismatch``）。缺省让框架用
``LocalToolHost(真注册表)``。

用法：``python -m paperpilot.mecha_adapter.hub [--config 路径] [--port 0]
[--data-dir mecha] [--mode open|locked|human|ai] [--port-file 路径]``
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from mecha.assembly import assemble
from mecha.authority import Mode
from mecha.data_layout import resolve_data_dir
from mecha.providers.mcp import McpEndpoint as _McpEndpoint
from mecha.providers.mcp import build_mcp_server as _framework_build_mcp_server
from mecha.site import initial_mode_from_env

from .commands import build_commands, invoke_command
from .engine import CONFIG_SCHEMA, make_validator
from .monitor import paperpilot_summarizer, start_cockpit
from .scopes import build_scope_policy
from .tools import build_required_source, build_tool_registry

#: MCP 服务名 → 宿主侧工具名形如 ``mcp__paperpilot__read_paper``。
MCP_SERVER_NAME = "paperpilot"

#: 服务端说明（模型可见）。只含任务概念，不含 UI/传输/实现词汇；体积由框架的
#: ``check_instructions`` 在构造期守卫（宿主侧 maxInstructionBytes 默认 32768）。
INSTRUCTIONS = (
    "PaperPilot 论文情报工具集。每日简报的协作流程："
    "① fetch_papers 抓取新论文入库（遵守 arXiv 限速，稍慢）；"
    "② prepare_review 取过规则后的候选清单（含主题画像与基线分）；"
    "③ 你逐篇评审（score/label/reason，入选者给结构化 summary），submit_review 一次性提交；"
    "④ finalize_briefing 生成简报。也可 run_pipeline 一键跑（无评审时用启发式兜底）。"
    "检索用 search_papers / read_paper；主题管理用 add_topic / set_topic_enabled；"
    "引文追溯用 paper_metrics / read_references / read_citations。"
    "写入带归因与可追溯留痕，read_activity 可查『谁改了什么』；"
    "写错了用 undo_change(seq=0) 撤销最近一条可逆操作（入库/定稿不可逆，会明说）。"
    "所有错误可教学（kind/hint/suggest），按提示改，别盲试。"
)


def _seed_config(gate, container) -> None:
    """装配期把 settings.yaml 的标量配置种进 Gate（seed 不经写权门）。

    种子后 gate 快照即配置态的权威（cockpit/Monitor 可见、set_config 可改、
    Engine.run 叠加生效）。actor='bootstrap' 区别于 human/ai 的操作者写。
    """
    s = container.settings
    values = {
        "lookback_days": s.lookback_days,
        "scoring.threshold": s.scoring.threshold,
        "scoring.quota_per_topic": s.scoring.quota_per_topic,
        "scoring.max_papers": s.scoring.max_papers,
        "scoring.max_per_author": s.scoring.max_per_author,
        "scoring.must_read_cap": s.scoring.must_read_cap,
    }
    for key in CONFIG_SCHEMA:               # 只种声明过的配置键（单一来源）
        if key in values:
            gate.seed(key, values[key], actor="bootstrap",
                      reason="seed from settings.yaml")


def build_stack(container, data_root: str | Path | None = None,
                data_dir_name: str = "mecha", *, mode: Mode = Mode.LOCKED) -> dict:
    """装配 PaperPilot 的 v2 栈——**经 mecha.assembly.assemble() 具名装配点**。

    接线顺序（engine 先于 gate 建成，故 attach_gate 回填）：
    assemble → engine.attach_gate → build_commands（``TOOL_DECLS`` 的写声明派生的命令全进
    sw.commands，另有几条人类专属命令）→ _seed_config（标量配置种进 gate）
    → build_tool_registry（工具面）。

    ⚠ **本仓不手抄计数**：各面的条数以 ``check/test_capability_map.py`` 的期望表为准。

    返回里**只带真注册表**（``tools``），**不带** ``toolhost``（见模块 docstring）。
    ``data_root`` 缺省 = 项目数据目录（``settings.data_dir``）；journal 落
    ``<data_root>/<data_dir_name>/journal.jsonl``。
    """
    root = Path(data_root) if data_root is not None else Path(container.settings.data_dir)
    sw = assemble(root=root, data_dir_name=data_dir_name,
                  summarizer=paperpilot_summarizer, validate=make_validator(),
                  mode=mode, project="paperpilot",
                  # ⭐ **账的落地点换成 GP 自己的库**（2026-09-30）：账必须住在**状态旁边**——
                  # GP 的域状态在 SQLite 里，只有同库同事务才能做到"状态变了、账不可能没记"。
                  # ⇒ "配置写 / 域写 / 命令审计"共用 `events` 表、**同一个序列**
                  # （实现：`infra/ledger.py::SqlLedger`；框架出厂默认是本机 JSONL）。
                  log_sink=container.repo.ledger)
    ai = sw.channels["ai"]
    command_names = build_commands(container, sw)
    # ⭐ **把 scope 声明的"插头"插上**（2026-09-26）：框架那段作用域过滤只在注入了
    # `ScopePolicy` 时才生效（`mecha/commands.py:432`）⇒ 不绑 = **声明在、消费者在、插头没插**
    # （实测：`add_topic` 经 AI 工具面静默成功）⇒ 框架守卫红不了，只能项目侧判据抓。
    # ⚠ 本批绑定**在效果上近乎空操作（除 `profile`）**：价值在"声明变规则 + 给判据反例 +
    #    留收紧开关"——**别读成"补了个大洞"**（详见 `.scopes` 模块 docstring）。
    sw.commands.bind_scope_policy(build_scope_policy())
    _seed_config(sw.gate, container)
    tools = build_tool_registry(container, sw, ai)
    return {
        "software": sw, "container": container,
        "gate": sw.gate, "history": sw.history, "surface": sw.surface,
        "monitor": sw.monitor, "authority": sw.authority, "jobs": sw.jobs,
        "commands": sw.commands, "command_names": command_names,
        "channels": sw.channels, "ai": ai, "tools": tools,
        "required_source": build_required_source(container),
    }


def build_mcp_server(tools, *, required_source=None, toolhost=None, **kwargs):
    """按 PaperPilot 默认值**纯构造** MCP 服务（不起线程、不碰端口；判据可直调）。"""
    return _framework_build_mcp_server(
        tools, server_name=MCP_SERVER_NAME, instructions=INSTRUCTIONS,
        toolhost=toolhost, required_source=required_source, **kwargs)


class McpHost:
    """在权威进程后台线程跑 MCP 服务（薄壳：委托框架 ``McpEndpoint``）。

    默认那组 ``server_name`` / ``instructions`` 就是**AI 能力面**；⚠ **控制端点复用同一个壳**、
    只是换这两个值 + 换端口文件（见 ``control.py``）—— 不为第二条端点再写一层壳。
    """

    def __init__(self, tools, *, server_name: str = MCP_SERVER_NAME,
                 instructions: str = INSTRUCTIONS, required_source=None, toolhost=None,
                 host: str = "127.0.0.1", port: int = 0, port_file=None, log=None) -> None:
        self._endpoint = _McpEndpoint(
            tools, server_name=server_name, instructions=instructions,
            toolhost=toolhost, required_source=required_source,
            host=host, port=port, port_file=port_file, log=log)

    @property
    def port(self) -> int | None:
        return self._endpoint.port

    @property
    def url(self) -> str:
        return self._endpoint.url

    @property
    def server(self):
        return self._endpoint.server

    def build_server(self):
        return self._endpoint.build_server()

    def start(self, timeout: float = 30.0) -> int:
        return self._endpoint.start(timeout)

    def stop(self, timeout: float = 10.0) -> None:
        self._endpoint.stop(timeout)

    def __enter__(self) -> McpHost:
        self.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False


def make_host(stack: dict, *, host: str = "127.0.0.1", port: int = 0,
              port_file=None, log=None) -> McpHost:
    """由 ``build_stack`` 的产物造一台 MCP 端点壳（真入口与判据同源，不传 toolhost）。"""
    return McpHost(stack["tools"], required_source=stack["required_source"],
                   host=host, port=port, port_file=port_file, log=log)


def human_write(stack: dict, cmd_name: str, **args) -> dict:
    """人类面（Web）经**同一道门**写：用 human 通道 invoke 命令。

    ⚠ **抢占已删**（2026-09-26 行为变更）：从前这里会"发现模式不是 HUMAN 就切到 HUMAN"
    （人写优先）。现在**不再动模式**——理由两条：
    ① 用户裁决「不卡写权」⇒ 起步就是 `Mode.OPEN`（两侧都能写，见 `BOOT_MODE_DEFAULT`）；
    ② **一个随时会被自己改掉的模式不是模式**：抢占让模式不可信。
    ⇒ 于是 `AI`/`LOCKED` 之下人类写会被**框架拒**（`authority_mode_mismatch`/`authority_locked`），
    拒绝消息里给得出路（在 `/settings` 切回"放开"）——**拒绝 ≠ 卡死**。
    actor 仍由 human 通道钉死，写落 repo.events（actor=human）+ mecha History
    （`command.<name>` 审计）——与 AI 写同一条门、同一审计面。返回归一化回执。
    """
    args.setdefault("reason", "")
    # `approval=sw.approval`：把审批通道接上（框架对 `approval_required` 的命令 fail-closed；
    # 本项目暂无命令声明它 ⇒ 今天不触发，但接线先做对——将来写上声明即生效）。
    return invoke_command(stack["commands"], stack["gate"],
                          stack["channels"]["human"], cmd_name, args,
                          approval=stack["software"].approval)


#: 起步写权模式的**默认值**：`open` = 两侧都能写（用户裁决"不卡写权"）。
#: ⚠ 框架的出厂默认仍是 `LOCKED`（安全默认没动）——**"GP 从哪起步"由本项目声明**。
BOOT_MODE_DEFAULT = "open"

#: 模式名 → 框架 `Mode`。四个值都能起步；`locked` 是保留的**急停/维护态**。
_BOOT_MODES = {"open": Mode.OPEN, "locked": Mode.LOCKED, "human": Mode.HUMAN, "ai": Mode.AI}


def _ensure_printable_stdout() -> None:
    """让框架那行"回落 + 出声"**打得出来**。

    ⚠ **实测**：`initial_mode_from_env` 的提示里带 `⚠`，而 Windows 默认控制台是 cp936
    ⇒ 非法值那条路会**抛 `UnicodeEncodeError`**（本该"回落 + 出声"，实际崩掉）。
    两个 CLI 入口本来就做同款处置（stdout 统一 UTF-8）；本函数让**库路径**（判据/嵌入调用）也一样稳。

    ⚠ 这**不是第二份判定**：回落规则与那句话仍由框架给；这里只保证那个 print 有地方落。
    """
    stream = sys.stdout
    if str(getattr(stream, "encoding", "")).lower().replace("-", "") == "utf8":
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:                  # pytest 的 capture 流没有它 ⇒ 它的编码本来就安全
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):            # 已关闭/已被替换的流：尽力而为，不拦启动
        pass


def boot_mode(value: str = "") -> Mode:
    """起步写权模式。⭐ **优先级：argv 显式 > `MECHA_INITIAL_MODE`（框架声明）> 本仓默认**。

    三级各自的性质不同（**别混 —— 它们不冲突**）：

    | 级 | 谁给的 | 语义 | 非法值怎么办 |
    |---|---|---|---|
    | ① `--mode`（argv） | **人**当场敲的 | 本次启动的命令行选择 | **响亮抛**（敲错了要当场知道；静默回落 = 我以为锁上了其实没设防） |
    | ② `MECHA_INITIAL_MODE` | **框架 / 启动者** | "**这次启动**给什么写权"（从 mecha 启动 ⇒ 声明 AI） | **回落并出声**（框架 `initial_mode_from_env` 负责那行 print） |
    | ③ `BOOT_MODE_DEFAULT="open"` | **本项目** | "**软件出厂策略**"（用户裁决"不卡写权"） | —— |

    ⚠ **②与③不打架**：②说的是"**这一次**"，③说的是"**没有任何声明时**"。
    缺省 / 空串的 `MECHA_INITIAL_MODE` 都算"没声明"（空串 = **显式取消**）⇒ 落到 ③。

    为什么单独一层纯函数：`--mode` 是**给用户的 CLI 契约**，`MECHA_INITIAL_MODE` 是**框架契约**，
    而 `Mode` 是框架枚举 —— 隔一层，判据就能**不起服务**地验三档优先级。
    """
    key = (value or "").strip().lower()
    if key:                                  # ① 显式：非法**抛**（不回落）
        mapped = _BOOT_MODES.get(key)
        if mapped is None:
            raise ValueError(f"未知起步模式 {value!r}；可选：{'、'.join(_BOOT_MODES)}")
        return mapped
    # ②③：交给框架的读取口（缺省/空串 ⇒ 落回 ③；非法 ⇒ 回落 ③ **并出声**）。
    _ensure_printable_stdout()               # ⚠ 否则那行带 `⚠` 的提示在 GBK 控制台上会炸（实测）
    return initial_mode_from_env(_BOOT_MODES[BOOT_MODE_DEFAULT])


def _arg_parser() -> argparse.ArgumentParser:
    """`paperpilot-mecha` 的参数面（**单独一个函数**：判据可以不起服务地检查 flag 契约）。"""
    ap = argparse.ArgumentParser(prog="paperpilot-mecha",
                                 description="PaperPilot 的 mecha v2 MCP Hub")
    ap.add_argument("--config", default=None, help="settings.yaml 路径（默认自动发现）")
    ap.add_argument("--data-root", default="", help="journal 数据目录父（默认=项目 data 目录）")
    ap.add_argument("--data-dir", default="mecha", help="数据目录名（journal 落此处）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="0=自动选空闲端口")
    ap.add_argument("--port-file", default="",
                    help="端口文件（默认 <项目根>/.mcp-port，与发现约定一致）")
    # ⚠ **取代旧的 `--open-gate`**（那个 flag 的行为是"收成只有 AI 能写"，而默认已是 open
    # ⇒ 名字会撒谎）。四个值都保留能力：open=两侧都能写（默认）/ locked=都不写（维护态）/
    # human=只有人能写 / ai=只有 AI 能写。
    ap.add_argument("--mode", default="", choices=sorted(_BOOT_MODES),
                    help="起步写权模式：open=两侧都能写（默认）、locked=都不写（维护态）、"
                         "human=只有人能写、ai=只有 AI 能写。"
                         "⚠ 不传 ⇒ 让 boot_mode 解析（框架的 MECHA_INITIAL_MODE 优先于本仓默认）")
    ap.add_argument("--cockpit", action="store_true",
                    help="同时起 cockpit 只读监控端点（供 dsh 侧边栏/面板轮询）")
    ap.add_argument("--cockpit-port", type=int, default=0,
                    help="cockpit 端口（0=自动；库内零端口字面量，勿硬编 3080）")
    ap.add_argument("--cockpit-port-file", default="",
                    help="cockpit 端口发现文件（默认 <项目根>/.cockpit-port）")
    return ap


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from ..app.container import build_container
    from ..config import load_settings

    args = _arg_parser().parse_args(argv)

    container = build_container(load_settings(args.config))
    data_root = args.data_root or str(container.settings.data_dir)
    stack = build_stack(container, data_root, args.data_dir, mode=boot_mode(args.mode))
    # ⚠ 出处要说清（argv 显式 / 框架声明 / 本仓默认）—— 否则"为什么是 ai"没人知道。
    source = f"--mode {args.mode}" if args.mode else (
        "MECHA_INITIAL_MODE" if os.environ.get("MECHA_INITIAL_MODE", "").strip()
        else f"本仓默认 {BOOT_MODE_DEFAULT}")
    print(f"[hub] 起步写权模式：{stack['authority'].mode.value}（来自 {source}）")

    port_file = args.port_file or str(Path(data_root).parent / ".mcp-port")
    host = make_host(stack, host=args.host, port=args.port, port_file=port_file, log=print)
    port = host.start()
    journal = resolve_data_dir(Path(data_root), args.data_dir).data_dir
    print(f"[hub] {port_file} ← {port}；工具 {len(stack['tools'].schemas())} 个；journal={journal}")

    cockpit = None
    if args.cockpit:
        cp_file = args.cockpit_port_file or str(Path(data_root).parent / ".cockpit-port")
        cockpit = start_cockpit(stack, host=args.host, port=args.cockpit_port,
                                port_file=cp_file, log=print)
        print(f"[hub] cockpit {cp_file} ← {cockpit.port}（只读：/status /activity "
              f"/history /config /summary）")

    # ⭐ 控制端点（人侧专用，契约 §7.1.1）：**不门控**（写权与"有没有窗口"正交 ⇒
    # 每个 mcp 进程都起）。⭐ 机制在框架（`mecha.software_control`），这里只接线 +
    # 三个回调（`start_control`）。⚠ **不挂 log**：端口号不许进任何日志；起不来由框架抛（响亮）。
    from mecha.software_control import CONTROL_PORT_FILE

    from .control import start_control

    control_file = str(Path(data_root).parent / CONTROL_PORT_FILE)
    control, _cport = start_control(stack["authority"], gate=stack["gate"],
                                    port_file=control_file, host=args.host, log=None)
    print(f"[hub] 控制端点已起（端口只写 {control_file}，不外泄给 AI）")
    try:
        threading.Event().wait()                 # 阻塞到 Ctrl+C / terminate
    except KeyboardInterrupt:
        print("\n[hub] 收到 Ctrl+C，收尾（按内容清端口文件）。")
    finally:
        if cockpit is not None:
            cockpit.stop()
        control.stop()
        host.stop()
        stack["software"].close()
    return 0


__all__ = ["INSTRUCTIONS", "MCP_SERVER_NAME", "McpHost", "build_mcp_server",
           "build_stack", "human_write", "make_host"]


if __name__ == "__main__":
    sys.exit(main())
