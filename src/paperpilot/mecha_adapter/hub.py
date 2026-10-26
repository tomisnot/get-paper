"""进程入口：一台软件，四个面。

| 面 | 端点 | 谁用 |
|---|---|---|
| AI 能力面 | MCP streamable HTTP（`/mcp`，端口写 `.mcp-port`） | dsh |
| 监控面 | cockpit 只读（端口写 `.cockpit-port`） | 面板 |
| 控制面 | 写权/开手动面（端口写 `.control-port`） | 框架 / 面板 |
| 人面 | Web 工作台（固定端口） | 人 |

四个面**同一进程、同一套门、同一本账**。端口一律 `port=0` 由 OS 分配后回读，
数字只进端口文件——不打印、不进日志、不给 AI 看。
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
from pathlib import Path

from mecha.authority import Mode
from mecha.providers.mcp import McpEndpoint
from mecha.software_control import (CONTROL_PORT_FILE, build_control_registry,
                                    start_control_endpoint)

from ..config import load_settings
from ..context import build_context
from .monitor import start_cockpit
from .stack import Stack, boot_mode, build_stack
from .tools import INSTRUCTIONS

MCP_SERVER_NAME = "paperpilot"
MCP_PORT_FILE = ".mcp-port"
COCKPIT_PORT_FILE = ".cockpit-port"


def port_free(host: str, port: int) -> bool:
    """这个端口现在能 bind 吗（起 Web 前的**响亮**前置检查）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, int(port)))
            return True
        except OSError:
            return False


def start_mcp(stack: Stack, *, port_file: Path, log=None) -> McpEndpoint:
    endpoint = McpEndpoint(stack.tools, server_name=MCP_SERVER_NAME,
                           instructions=INSTRUCTIONS, host="127.0.0.1", port=0,
                           port_file=str(port_file), log=log)
    endpoint.start()
    return endpoint


def start_control(stack: Stack, *, port_file: Path, panel_url: str, log=None):
    def switch(mode: str) -> None:
        stack.authority.switch_mode(Mode(mode), side="human")

    def audit(op: str, target: str, reason: str) -> None:
        # 切写权本身不过门（否则切到 ai 之后人侧再也切不回来），但**必须留史**。
        stack.gate.record(op, target, reason, "human", reason)

    registry = build_control_registry(
        stack.authority, switch=switch, audit=audit,
        open_surface=lambda: panel_url,
        how_to_open=f"打开 Web 工作台 {panel_url} 的「设置」页切写权；"
                    f"AI 侧拿不到这个地址。")
    return start_control_endpoint(registry, port_file=str(port_file),
                                  server_name=f"{MCP_SERVER_NAME}-control", log=log)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paperpilot")
    parser.add_argument("--settings", default=None, help="settings.json 路径")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 data/）")
    parser.add_argument("--root", default=None, help="软件根（端口文件落这里）")
    parser.add_argument("--mode", default="", help="起步写权：open/locked/human/ai")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="建库并打印状态")
    sub.add_parser("tools", help="列出 AI 能力面")
    call = sub.add_parser("call", help="调一个工具（与 AI 走同一条门）")
    call.add_argument("name")
    call.add_argument("-p", "--params", default="{}")
    serve = sub.add_parser("serve", help="起软件（四个面）")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--no-web", action="store_true", help="只起 AI/监控/控制三个面")
    #: 开不开浏览器：`auto`（默认）= 被框架拉起时开、手动在终端跑时不开。
    #: 手动跑却想开就 `--open`，被框架拉起却不想开就 `--no-open`。
    serve.add_argument("--open", dest="open_browser", action="store_const", const="open",
                       help="起完就打开浏览器")
    serve.add_argument("--no-open", dest="open_browser", action="store_const", const="no",
                       help="不要打开浏览器")
    serve.set_defaults(open_browser="auto")
    return parser


def should_open_browser(flag: str = "auto", env: dict | None = None) -> bool:
    """要不要在起完之后把工作台推到人眼前。

    默认 `auto`：**被框架拉起时开**（框架会注入 `MECHA_SOFTWARE_ROOT` /
    `MECHA_INITIAL_MODE`，这两个变量在场就说明"这次启动是编排的一部分"），
    手动在终端跑时不开（那时人就在终端里，复制 URL 即可，不该凭空多一个标签页）。
    """
    source = os.environ if env is None else env
    if flag == "open":
        return True
    if flag == "no":
        return False
    return bool(str(source.get("MECHA_SOFTWARE_ROOT", "")).strip()
                or str(source.get("MECHA_INITIAL_MODE", "")).strip())


def open_when_ready(url: str, *, host: str = "127.0.0.1", port: int = 0,
                    timeout: float = 30.0) -> None:
    """等端口真的在听再开浏览器（否则会打开一个"无法访问"的页）。

    轮询 TCP 连接，超时就放弃并出声——**不静默**：打不开是因为服务没起来，
    这本身是该被看见的事实。
    """
    import socket
    import time
    import webbrowser

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.3)
    print(f"[paperpilot] 工作台 {url} 在 {timeout:.0f}s 内没起来，没有打开浏览器")


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings(Path(args.settings) if args.settings else None)
    if args.data_dir:
        settings.data_dir = args.data_dir
    ctx = build_context(settings=settings)
    stack = build_stack(ctx, root=args.root,
                        mode=boot_mode(args.mode) if args.mode else None)

    try:
        if args.command == "init":
            print({"db": str(ctx.db_path), "root": str(stack.root),
                   "mode": stack.mode, "tools": len(stack.tools.schemas()),
                   "commands": len(stack.command_names)})
            return 0
        if args.command == "tools":
            for schema in stack.tools.schemas():
                params = ", ".join(sorted((schema.get("parameters") or {})))
                print(f"{schema['name']}({params})  {schema['description']}")
            return 0
        if args.command == "call":
            import json
            try:
                params = json.loads(args.params or "{}")
            except ValueError as exc:
                print(f"参数不是合法 JSON：{exc}")
                return 2
            result = stack.tools.execute(args.name, params)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if not result.get("is_error") else 1
        if args.command == "serve":
            return _serve(stack, args)
    finally:
        if args.command != "serve":
            stack.close()
    return 1


def _serve(stack: Stack, args) -> int:
    root = stack.root
    host = args.host or stack.settings.host
    port = args.port or stack.settings.port
    panel_url = f"http://{host}:{port}"

    # 先验端口再起别的面：半启动状态（工具面起来了、人面没有）比直接失败更难查——
    # 这正是"面板能开但内容是旧的"那类假绿的来源。
    if not args.no_web and not port_free(host, port):
        print(f"[paperpilot] 端口 {port} 已被占用，Web 起不来。"
              f"先关掉占它的进程（可能是上一轮启动的实例），再重试。")
        stack.close()
        return 2

    mcp = start_mcp(stack, port_file=root / MCP_PORT_FILE, log=print)
    cockpit = start_cockpit(stack, port_file=root / COCKPIT_PORT_FILE, log=print)
    control, _cport = start_control(stack, port_file=root / CONTROL_PORT_FILE,
                                    panel_url=panel_url, log=print)
    print(f"[paperpilot] 写权 {stack.mode}；工具 {len(stack.tools.schemas())} 个；"
          f"端口写入 {root}")

    try:
        if args.no_web:
            threading.Event().wait()
        else:
            import uvicorn

            from ..web import create_app

            print(f"[paperpilot] 工作台 {panel_url}")
            if should_open_browser(getattr(args, "open_browser", "auto")):
                threading.Thread(
                    target=open_when_ready, args=(panel_url,),
                    kwargs={"host": host, "port": port}, daemon=True).start()
            uvicorn.run(create_app(stack), host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        control.stop()
        cockpit.stop()
        mcp.stop()
        stack.close()
    return 0
