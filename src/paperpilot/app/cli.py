"""CLI：paperpilot（无参数 = 统一启动 Web + MCP + cockpit + 每日调度）。

常用：
  paperpilot / serve           统一启动入口：Web（人类面）+ mecha MCP（AI 面）+ cockpit（监控面）+ 调度（共享一个 mecha 栈）
  paperpilot fetch [--days 3]   抓取论文入库
  paperpilot run [--force]      立即跑一次每日流水线
  paperpilot demo               离线演示（内置样例，不联网）
  paperpilot web [--port 8080]  只启动 Web（不接 mecha 栈；无监控面）
  paperpilot mcp                只起 mecha MCP 语义通道（headless，给 DSH 等 harness attach）
  paperpilot ai                 AI 模式：后台起 Web+MCP+cockpit，前台跑 dsh（AI 在 dsh 里驱动）
  paperpilot dsh-config         只读诊断：打印 dsh 组合后配置（验证隔离 overlay）
  paperpilot backup             备份 data/ 为 zip
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

import typer

from ..infra.arxiv import parse_atom

try:
    from ..mecha_adapter.hub import BOOT_MODE_DEFAULT
except ImportError as _exc:                      # 只拦"缺 mecha"这一种（别的原样抛）
    # ⭐ **可教学错误**（本仓给陌生人用：一条看不懂的 traceback 就是门槛）。
    # ⚠ 只对"缺 mecha（框架）"给教学信息；**别的 ImportError 原样抛**（别掩盖真错误）。
    #    判据口径：`ModuleNotFoundError.name` 是 `mecha` 或 `mecha.*` ⇒ 才算"缺框架"。
    if getattr(_exc, "name", "") != "mecha" and not str(getattr(_exc, "name", "")).startswith("mecha."):
        raise
    raise ImportError(
        "本入口需要 mecha（AI 接入框架），但当前环境没有装它。\n"
        "  装法：pip install -e '.[dev,mecha]'\n"
        "    · 他机/发布：走 GitHub 直接 URL（钉 v0.2.0 tag）；\n"
        "    · 本机开发：先 editable 装**兄弟目录**里的 mecha（两仓同级），再装本仓。\n"
        "  ⚠ 只想跑纯 Web 界面（无 AI/监控/写权门）时，**本入口仍会 import mecha** ⇒ 也得装上；\n"
        "    （`create_app(container)` 的无栈形态本身不依赖 mecha，见 tests/test_config_fallback.py 一带的实测口径。）\n"
        "  详见 README「怎么装」。"
    ) from _exc

#: `--mode` 的帮助文案（四个值逐一说清；尤其 `ai` = **只有 AI 能写**，别让名字猜）。
_MODE_HELP = ("起步写权模式：open=两侧都能写（默认，不卡写权）、locked=谁都不能写（维护/急停）、"
              "human=只有 Web 人侧能写、ai=只有 AI 能写")

app = typer.Typer(
    help="PaperPilot — arXiv 每日文献情报系统",
    no_args_is_help=False,
    add_completion=False,
)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

DEMO_XML = Path(__file__).resolve().parents[1] / "data" / "sample_arxiv.xml"
# ⚠ 本文件在 `src/paperpilot/app/` ⇒ 项目根 = `parents[3]`。`config.py` 在 `src/paperpilot/`，
# 那里的 `parents[2]` 才是项目根——照抄它会**少一层**，于是 `DSH_DIR` 指向 `<项目>/src/dsh`
# （不存在）：两个 `--patch` 全被静默丢掉、插件根本不加载，而现象只是"面板打不开 / 会话头部
# 什么都没有"。由 `_resolve_dsh_patches` 的响亮失败 + `tests/test_cli_startup.py` 的守卫钉住。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DSH_DIR = PROJECT_ROOT / "dsh"

#: Web 的**端口发现文件**（项目根，与 `.mcp-port` / `.cockpit-port` 同级）。
#: ⚠ 必须与 dsh 插件参数块 `dsh/src/panel/panel-config.ts` 的 `PANEL_CONFIG.PORT_FILE`
#: 一致——两边各写一份就是"漂移即面板空白"，故 `tests/test_cli_startup.py` 有一条判据
#: 直接读那个 .ts 比对。
WEB_PORT_FILE = ".web-port"


def _web_port_path(settings) -> Path:
    """Web 端口发现文件路径（项目根；`data_dir` 的父目录）。"""
    return Path(settings.data_dir).parent / WEB_PORT_FILE


def _publish_web_port(port_file: Path, port: int) -> None:
    """发布 Web 端口：**先清陈旧、再写自己的**（规则来自框架 `mecha.portfile`）。

    为什么先清（`expected=None` 无条件删）：`terminate()` / `taskkill /F` 不跑 Python 的
    `finally` ⇒ "起时写、止时删"不能只靠收尾；此刻本进程已装配成功（同一个项目根不可能
    还有别的活宿主）⇒ 该文件必属死进程，删它不会误伤。
    """
    from mecha.portfile import clear_port_file, write_port_file

    clear_port_file(port_file)
    write_port_file(port_file, port)


def _unpublish_web_port(port_file: Path, port: int) -> None:
    """收尾：**只删仍是自己写的那个端口**的文件（别误删接管者刚写的新端口）。"""
    from mecha.portfile import clear_port_file

    clear_port_file(port_file, expected=port)


def _publish_web_port_when_ready(host: str, port: int, port_file: Path,
                                 *, timeout: float = 25.0) -> None:
    """**端口真的在听之后**才发布端口文件（后台线程，尽力而为）。

    为什么不"启动前就写"：写了但还没 listen ⇒ 面板拿到一个连不上的地址，与"权威没起来"
    显示成"连上了但空白"是同一族假绿。起不来就**不写** ⇒ 地址路由 503 + 可读错误
    （面板显示"地址不详 + 为什么"），而不是给个死地址。
    0.0.0.0 等回环绑定按 127.0.0.1 探活。
    """
    import socket
    import threading
    import time

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "", "*") else host

    def _wait_and_publish() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                socket.create_connection((probe_host, port), timeout=0.3).close()
            except OSError:
                time.sleep(0.2)
                continue
            _publish_web_port(port_file, port)
            return

    threading.Thread(target=_wait_and_publish, daemon=True).start()


def _ensure_port_free(host: str, port: int, label: str) -> None:
    """预检端口可绑定；被占则**响亮失败**（不半启动）。

    为何必要：Web 在后台线程跑 uvicorn，bind 失败只会在那个线程里报错然后静默死掉，
    主流程却继续拉 MCP/cockpit/dsh ⇒ 留下“dsh 起了、Web 死了”的半残态（实测：
    Errno 10048 端口被占）。先在启动前探一下，占了就把可操作的排障步骤说清再退。
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        typer.echo(
            f"❌ {label}端口 {host}:{port} 已被占用——多半是上一次 PaperPilot 没退干净，"
            "或别的服务在用它。", err=True)
        typer.echo(f"   查占用：netstat -ano | findstr :{port}   → 末列是 PID", err=True)
        typer.echo("   释放它：taskkill /F /PID <pid>（确认那是残留的 PaperPilot/uvicorn 再杀）", err=True)
        typer.echo("   或换端口：serve 用 --port；ai 改 config/settings.yaml 的 web.port"
                   "（dsh 面板 webUrl 需同步）。", err=True)
        raise typer.Exit(code=1) from None
    finally:
        probe.close()


def _open_browser_when_ready(host: str, port: int, path: str = "",
                             timeout: float = 25.0) -> None:
    """后台轮询 Web 就绪后自动开浏览器——把“人面”变成前台可见（而非隐在后台）。

    Web 本质是本地服务（无独立窗口），浏览器就是它的前台；不自动开用户就不知道
    去哪用。轮询端口连上后 `webbrowser.open`；`--no-open` 可关。0.0.0.0 等回环用 127.0.0.1。
    """
    import socket
    import threading
    import time
    import webbrowser

    url_host = "127.0.0.1" if host in ("0.0.0.0", "", "*") else host
    url = f"http://{url_host}:{port}/{path.lstrip('/')}"

    def _wait_and_open() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                socket.create_connection((url_host, port), timeout=0.3).close()
            except OSError:
                time.sleep(0.2)
                continue
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - 开不了浏览器不影响服务已起
                pass
            return

    threading.Thread(target=_wait_and_open, daemon=True).start()


def _dsh_patch_paths() -> tuple[list[Path], list[Path]]:
    """dsh overlay patch：返回 ``(存在的, 缺失的)``。

    顺序有意义（`isolate` 必须落在组合链**最后一层**，端口 `!!js` 表达式才生效），故保序。
    """
    wanted = (DSH_DIR / "cordis.source.patch.yml", DSH_DIR / "cordis.isolate.patch.yml")
    return [p for p in wanted if p.exists()], [p for p in wanted if not p.exists()]


def _resolve_dsh_patches() -> list[Path]:
    """取 dsh overlay patch，**缺任何一份都响亮失败**（绝不静默少传）。

    为什么不许静默过滤：**"少传了 patch"与"传了但没生效"在现象上一模一样**——插件不加载、
    会话头部没有按钮、工具面是空的。静默过滤会把「No plugin」伪装成「正常启动」，是本仓
    反复付出代价的那类失败。实测代价：`PROJECT_ROOT` 少一层 ⇒ 两个 patch 全被丢掉 ⇒
    用户报的"监控面板打不开"（真根因不在面板）。
    """
    patches, missing = _dsh_patch_paths()
    if missing:
        typer.echo("❌ dsh overlay patch 缺失——缺了它插件根本不会加载（面板 / AI 工具全没有），故不启动：",
                   err=True)
        for path in missing:
            typer.echo(f"   · {path}", err=True)
        typer.echo(f"   项目根判定 = {PROJECT_ROOT}"
                   "（若不对，就是本文件 PROJECT_ROOT 的层数写错）", err=True)
        raise typer.Exit(code=1)
    return patches


def _dsh_argv(patches: list[Path], port: int, *, no_open: bool = False) -> str:
    """拼 `dsh web` 的启动参数串（纯函数 ⇒ 判据能直接断言 patch 与端口真的在里面）。"""
    args = "web" + "".join(f' --patch "{p}"' for p in patches) + f" --port {port}"
    return args + (" --no-open" if no_open else "")


def _pick_dsh_port(preferred: int = 3081, tries: int = 10) -> int:
    """从 preferred 起挑第一个可绑定的回环端口；**3080 永不入选**（留给官方 dsh）。

    并存原理（mecha-sdk《DSH-实例并存原理.md》§9）：dsh 无单实例锁，硬单例只有监听
    端口；多项目实例各挑各的端口即可并存。先 bind 试探再释放，真正监听交给 dsh。
    """
    import socket

    for port in range(preferred, preferred + tries):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    return preferred


def _publish_and_announce_control_token() -> None:
    """生成并打印**人工控制口令**（`POST /monitor/mode` 切写权用）。

    ⚠ **刻意存在仓外**（`~/.paperpilot/control-token`）：它挡的是**本机其他进程**，
    **挡不住有权读你文件的 AI**。放仓外是为了让"AI 不能自授权"这条边界**尽量真的成立**
    （AI 的文件访问通常被限在工作区）；放项目根则会让"服务端钉 `actor=human`"退化成
    "AI 的写被记成人的写"——比没有鉴权更坏。写失败不抛：控制端点 fail-closed 兜住。
    """
    from .control_token import control_token_path, publish_control_token

    try:
        token = publish_control_token()
    except OSError as exc:  # noqa: BLE001 - 家目录不可写只影响控制端点，别拦启动
        typer.echo(f"⚠ 控制口令写入失败（{exc}）⇒ 写权切换会 fail-closed 一律拒绝。", err=True)
        return
    typer.echo(f"🔑 人工控制口令（切写权用）: {token}")
    typer.echo(f"   —— 存于仓外 {control_token_path()}")
    typer.echo("   —— ⚠ 它挡的是**本机其他进程**，挡不住能读你文件的 AI（这条边界是明说的）")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config: Path = typer.Option(None, "--config", "-c", help="配置文件路径"),
) -> None:
    # GBK 控制台炸弹（实测）：typer echo 打 emoji（🟢/⚪️/🌐/📦）在 cp936 直接
    # UnicodeEncodeError——与 hub.main 同款处置：stdout/stderr 统一 UTF-8 + replace。
    import sys
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    if ctx.invoked_subcommand is None:
        _serve_impl(config)


def _boot_stack(settings, *, mode: str, with_cockpit: bool, log):
    """建 container + **共享 mecha 栈**，起 MCP（AI 面）+ cockpit（监控面）后台线程。

    三面（Web 人类面 / MCP AI 面 / cockpit 监控面）**共享一个栈**（同一 authority/
    gate/history）——一个 data_dir 一个写租约，故必须同进程共栈。返回
    ``(container, stack, mcp_host, cockpit)``；调用方负责收尾（stop/close）。

    ⚠ **起步模式由 ``mode`` 决定**（默认 `open`，见 `hub.BOOT_MODE_DEFAULT`）：
    这是"GP 不卡写权"的落点；`locked` 仍是急停（人随时可切回去）。
    """
    from ..mecha_adapter.hub import boot_mode, build_stack, make_host
    from ..mecha_adapter.monitor import start_cockpit
    from .container import build_container

    container = build_container(settings)
    stack = build_stack(container, settings.data_dir, "mecha", mode=boot_mode(mode))
    root = Path(settings.data_dir).parent
    mcp_host = make_host(stack, host=settings.mcp.host, port=0,
                         port_file=str(root / ".mcp-port"), log=log)
    mcp_host.start()
    cockpit = None
    if with_cockpit:
        cockpit = start_cockpit(stack, host=settings.web.host, port=0,
                                port_file=str(root / ".cockpit-port"), log=log)
    return container, stack, mcp_host, cockpit


def _serve_impl(config, host=None, port=None, mode=BOOT_MODE_DEFAULT, no_cockpit=False,
                no_open=False) -> None:
    """统一启动实体（被 no-arg 默认与 ``serve`` 命令共用；普通默认值，非 Option）。"""
    import uvicorn

    from ..config import load_settings
    from .web import create_app

    settings = load_settings(config)
    web_host = host or settings.web.host
    web_port = port or settings.web.port
    web_port_file = _web_port_path(settings)
    _ensure_port_free(web_host, web_port, "Web ")   # 预检：端口被占则不半启动
    _publish_web_port_when_ready(web_host, web_port, web_port_file)
    container, stack, mcp_host, cockpit = _boot_stack(
        settings, mode=mode, with_cockpit=not no_cockpit, log=typer.echo)
    typer.echo(f"🌐 Web（人类面）: http://{web_host}:{web_port}  (AI: {container.ai_provider})")
    typer.echo(f"🔌 MCP（AI 面）: {mcp_host.url}  ← .mcp-port")
    if cockpit is not None:
        typer.echo(f"📊 cockpit（监控面）: {cockpit.url}  ← .cockpit-port")
    typer.echo(f"🧭 dsh 面板发现: {web_port_file.name} ← {web_port}（同源路由读它，不回落默认端口）")
    _publish_and_announce_control_token()
    typer.echo(f"🔐 写权模式: {stack['authority'].mode.value}"
               "（默认 open=两侧都能写；要独占/急停去 /settings 的写权卡切）")
    if not no_open:
        _open_browser_when_ready(web_host, web_port)   # 自动开浏览器 = 人面前台可见
        typer.echo("🖥  已尝试打开浏览器（人面工作台）；未弹出就手动访问上面的 Web 地址。")
    try:
        uvicorn.run(create_app(container, stack), host=web_host, port=web_port, log_level="info")
    except KeyboardInterrupt:
        typer.echo("\n[serve] 收到 Ctrl+C，收尾…")
    finally:
        if cockpit is not None:
            cockpit.stop()
        mcp_host.stop()
        stack["software"].close()
        _unpublish_web_port(web_port_file, web_port)


@app.command()
def serve(
    config: Path = typer.Option(None, "--config", "-c", help="配置文件路径"),
    host: str = typer.Option(None, "--host", help="Web 绑定地址（默认取 settings.web.host）"),
    port: int = typer.Option(None, "--port", "-p", help="Web 端口（默认取 settings.web.port）"),
    mode: str = typer.Option(BOOT_MODE_DEFAULT, "--mode", help=_MODE_HELP),
    no_cockpit: bool = typer.Option(False, "--no-cockpit", help="不起 cockpit 监控端点"),
    no_open: bool = typer.Option(False, "--no-open", help="不自动开浏览器（人面前台）"),
) -> None:
    """统一启动入口：Web（人类面）+ mecha MCP（AI 面）+ cockpit（监控面），共享一个 mecha 栈。

    人机同路：Web 写经 human 通道、AI 写经 ai 通道，同一道写权门 + 同一份审计。
    ⚠ **写权默认 `--mode open`**（两侧都能写，用户裁决"不卡写权"）；`--mode locked` 是急停。
    dsh 经 `.mcp-port` 发现 MCP；右栏「📄简报」iframe Web 可视化面、「◈监控」是原生面板
    （mecha cockpit；原 `/monitor` 视图页已退役，写权模式卡在 Web 的 `/settings`）。
    （无参数运行 `paperpilot` 等价于本命令。）
    """
    _serve_impl(config, host, port, mode, no_cockpit, no_open)


@app.command()
def fetch(
    days: int = typer.Option(3, "--days", "-d", help="抓取最近 N 天提交的论文"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """抓取 arXiv 新论文入库（遵守 3s 限速）。

    ⚠ **与 AI 同源**（2026-09-26 消重）：本命令走**同一个能力** `fetch_papers`，
    不再自己建 `ArxivClient` + 直写 repo——否则命令行与 AI 抓的**口径/计数会不一样**
    （去噪、`global_fallback`、域留痕都在能力里）。归因记 `cli`（不借 human 身份）。
    """
    from .. import capabilities
    from ..config import load_settings
    from .container import build_container

    settings = load_settings(config)
    out = capabilities.registry_for(build_container(settings)).invoke(
        "fetch_papers", days=int(days), actor="cli",
        reason=f"CLI 抓取最近 {days} 天论文")
    if not out.get("ok"):
        typer.echo(json.dumps(out, ensure_ascii=False, default=str))
        raise typer.Exit(code=1)
    typer.echo(f"抓取 {out.get('fetched', 0)} 篇：新增 {out.get('new', 0)}，"
               f"更新 {out.get('updated', 0)}")


@app.command()
def run(
    date_str: str = typer.Option(None, "--date", help="目标日期 YYYY-MM-DD，默认今天"),
    force: bool = typer.Option(False, "--force", help="当天已有简报也重跑"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """立即跑一次每日流水线（打分 → 精读 → 简报）。"""
    from datetime import date as date_cls

    from ..config import load_settings
    from .container import build_container

    settings = load_settings(config)
    container = build_container(settings)
    when = date_cls.fromisoformat(date_str) if date_str else None
    result = container.pipeline.run(
        when, force=force, actor="human", reason="CLI 手动跑流水线"
    )

    if result.error:
        typer.echo(f"❌ 失败: {result.error}", err=True)
        raise typer.Exit(code=1)
    if result.reused:
        typer.echo(f"ℹ️ {result.date} 简报已存在（--force 可重跑）")
        return
    typer.echo(
        f"✅ {result.date} 完成：候选 {result.fetched} → 过规则 {result.after_rules} → 入选 {result.selected}"
    )
    for msg in result.degraded:
        typer.echo(f"   ⚠️ {msg}")


@app.command()
def demo(config: Path = typer.Option(None, "--config", "-c")) -> None:
    """离线演示：用内置样例 Atom 灌库并跑完整流水线（不联网）。"""
    from ..config import load_settings
    from .container import build_container

    if not DEMO_XML.exists():
        typer.echo(f"找不到样例文件: {DEMO_XML}", err=True)
        raise typer.Exit(code=1)

    settings = load_settings(config)
    container = build_container(settings)
    papers = parse_atom(DEMO_XML.read_text(encoding="utf-8"))
    result = container.repo.upsert_papers(
        papers, actor="human", reason="demo 样例灌库"
    )
    reset = container.repo.reset_statuses(
        [p.arxiv_id for p in papers], actor="human", reason="demo 重置状态以便重跑"
    )
    typer.echo(
        f"样例入库 {len(papers)} 篇：新增 {result['new']}，更新 {result['updated']}"
        + (f"，重置 {reset} 篇状态（可重复演示）" if reset else "")
    )

    outcome = container.pipeline.run(
        force=True, actor="human", reason="demo 跑流水线"
    )
    if outcome.error:
        typer.echo(f"❌ 失败: {outcome.error}", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"✅ {outcome.date} 简报：候选 {outcome.fetched} → 过规则 {outcome.after_rules} → 入选 {outcome.selected}"
    )
    for msg in outcome.degraded:
        typer.echo(f"   ⚠️ {msg}")
    briefing = container.repo.briefing_for_date(outcome.date)
    if briefing:
        typer.echo(f"📄 简报 Markdown：{settings.data_dir / 'briefings' / (outcome.date + '.md')}")
        _write_briefing_file(settings, outcome.date, briefing.markdown)


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port", "-p"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """只启动 Web（不带每日调度；不接 mecha 栈 ⇒ `/settings` 的写权卡如实报「未接监控面」）。"""
    import uvicorn

    from ..config import load_settings
    from .container import build_container
    from .web import create_app

    settings = load_settings(config)
    container = build_container(settings)
    web_port_file = _web_port_path(settings)
    _publish_web_port_when_ready(host, port, web_port_file)
    typer.echo(f"🌐 http://{host}:{port}  (AI: {container.ai_provider})")
    typer.echo(f"🧭 dsh 面板发现: {web_port_file.name} ← {port}（同源路由读它）")
    try:
        uvicorn.run(create_app(container), host=host, port=port, log_level="info")
    finally:
        _unpublish_web_port(web_port_file, port)


@app.command()
def mcp(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(0, "--port", "-p", help="0=自动选空闲端口（写 .mcp-port 供发现）"),
    mode: str = typer.Option(BOOT_MODE_DEFAULT, "--mode", help=_MODE_HELP),
    no_cockpit: bool = typer.Option(False, "--no-cockpit", help="不起 cockpit 监控端点"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """只起 mecha MCP 语义通道（headless，无 Web）；DSH 等 harness 经 .mcp-port attach。"""
    from ..mecha_adapter import hub as mecha_hub

    argv = ["--host", host, "--port", str(port), "--mode", mode]
    if not no_cockpit:
        argv.append("--cockpit")
    if config:
        argv += ["--config", str(config)]
    raise typer.Exit(code=mecha_hub.main(argv))


@app.command()
def ai(
    config: Path = typer.Option(None, "--config", "-c"),
    no_open: bool = typer.Option(False, "--no-open", help="调试：dsh 不自动开浏览器"),
    dsh_port: int = typer.Option(0, "--dsh-port", help="调试：固定 dsh 端口（0=自动从 3081 选）"),
    debug_home: bool = typer.Option(
        False,
        "--debug-home",
        help="调试：用项目内 .dsh-debug 作 DSH_HOME（会话/设置/凭据与共享 ~/.dsh 完全隔离）",
    ),
    mode: str = typer.Option(BOOT_MODE_DEFAULT, "--mode", help=_MODE_HELP),
) -> None:
    """AI 模式：后台起 Web(:8080) + MCP 语义通道(写 .mcp-port)，**前台跑 dsh**。

    AI 对话与管理全部复用 DSH（harness）；PaperPilot 只暴露语义面（MCP 工具）
    与一个内嵌面板（dsh 插件）。Ctrl+C 收尾后台服务。

    **并存隔离**（mecha-sdk《DSH-实例并存原理.md》§9）：除插件源 patch 外再挂
    `dsh/cordis.isolate.patch.yml`（进程级最后一层）——禁掉钉端口/回写共享 profile 的
    `remote-web-ui`、并还原 webserver 端口表达式；端口自动从 3081 选（3080 留给官方
    dsh）⇒ 本实例与官方/其他项目的 dsh 互不影响。（该 overlay **只写本实例需要的隔离**，
    不列举别家的插件 id。）
    """
    import threading

    import uvicorn

    from ..config import load_settings
    from .web import create_app

    settings = load_settings(config)
    if not settings.mcp.enabled:
        typer.echo("⚠ mcp.enabled=false：AI 模式需要 MCP 语义通道，仍继续启动…")
    _ensure_port_free(settings.web.host, settings.web.port, "Web ")   # 预检：端口被占则不半启动
    web_port_file = _web_port_path(settings)
    _publish_web_port_when_ready(settings.web.host, settings.web.port, web_port_file)

    # 1) 后台：MCP（AI 面）+ cockpit（监控面），共享一个 mecha 栈；AI 模式默认开闸到 AI
    container, stack, mcp_host, cockpit = _boot_stack(
        settings, mode=mode, with_cockpit=True, log=typer.echo)
    typer.echo(f"  MCP（AI 面）: {mcp_host.url}（.mcp-port 已写；dsh 插件据此发现）")
    if cockpit is not None:
        typer.echo(f"  cockpit（监控面）: {cockpit.url}（.cockpit-port 已写）")
    typer.echo(f"  dsh 面板发现: {web_port_file.name} ← {settings.web.port}"
               "（插件经同源只读路由读它，不回落默认端口）")
    _publish_and_announce_control_token()

    # 2) 后台：Web 面板（dsh 右栏「📄简报」iframe 它；写权模式卡在 /settings）
    web_app = create_app(container, stack)
    web_server = uvicorn.Server(
        uvicorn.Config(web_app, host=settings.web.host, port=settings.web.port, log_level="warning")
    )
    web_thread = threading.Thread(target=web_server.run, daemon=True)
    web_thread.start()
    typer.echo(f"  Web 面板: http://{settings.web.host}:{settings.web.port}（后台）")
    if not no_open:
        _open_browser_when_ready(settings.web.host, settings.web.port)   # 人面前台可见

    def _cleanup() -> None:
        try:
            if cockpit is not None:
                cockpit.stop()
            mcp_host.stop()
            stack["software"].close()
        except Exception:  # noqa: BLE001 - 收尾尽力
            pass
        finally:
            _unpublish_web_port(web_port_file, settings.web.port)

    # 3) 前台：dsh harness（AI 界面 + 📄 PaperPilot 面板都在里面）
    dsh_exe = shutil.which("dsh")
    if dsh_exe is None:
        # 没有 dsh 也不空手而归：Web + MCP + cockpit 已在后台跑，前台阻塞到 Ctrl+C
        typer.echo("⚠  PATH 里没有 dsh——降级：Web + MCP + cockpit 已在后台跑。")
        typer.echo("   想用 AI 对话驱动，请先安装：npm i -g @deepseek-ai/dsh，再运行 paperpilot ai")
        typer.echo(f"   Web 面板: http://{settings.web.host}:{settings.web.port}；其它 harness 可 attach MCP（{mcp_host.url}）。Ctrl+C 退出。")
        try:
            _wait_forever()
        finally:
            _cleanup()
        return
    if DSH_DIR.is_dir() and not (DSH_DIR / "node_modules").is_dir():
        typer.echo("[launcher] dsh/node_modules 缺失 → 自动 npm install（首次约 15-60s）…")
        try:
            subprocess.run(
                "npm install --legacy-peer-deps --ignore-scripts "
                "--registry=https://registry.npmmirror.com",
                shell=True,
                cwd=str(DSH_DIR),
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"⚠ npm install 失败：{exc}（dsh 可能起不来）")

    patches = _resolve_dsh_patches()   # 缺 patch 响亮失败，绝不静默少传（见该函数说明）
    port = dsh_port or _pick_dsh_port()
    dsh_args = _dsh_argv(patches, port, no_open=no_open)
    env = None
    if debug_home:
        home = PROJECT_ROOT / ".dsh-debug"
        home.mkdir(exist_ok=True)
        env = {**os.environ, "DSH_HOME": str(home)}
        typer.echo(f"  [debug] DSH_HOME={home}（独立家目录；首次需重新登录，与共享 ~/.dsh 互不可见）")
    typer.echo(f"  前台启动 dsh（启动输出直接显示在本窗，请等它起来）: dsh {dsh_args}")
    typer.echo(f"  · 隔离 overlay 已挂：本实例端口 {port}（官方 3080 不受影响、共享 profile 不被回写）")
    typer.echo("  · dsh 起来后会打开浏览器 = AI 界面；📄 PaperPilot 面板在会话头部按钮里")
    try:
        # cwd = 项目根：`--patch` 与插件的三个端口发现文件（`.mcp-port` / `.web-port` /
        # `.cockpit-port`）都是**相对 cwd** 的约定 ⇒ 钉住 cwd 才不会"在别处运行就全部找不到"。
        rc = subprocess.run(f'"{dsh_exe}" {dsh_args}', shell=True, env=env, cwd=str(PROJECT_ROOT))
        if rc != 0:
            typer.echo(f"[launcher] ⚠ dsh 退出码 {rc}。常见原因：")
            typer.echo("  1) node_modules 缺依赖：cd dsh && npm install 后重试；")
            typer.echo("  2) EPERM ~/.dsh/profiles/web/cordis.yml：该 profile 被占用/无写权限")
            typer.echo("     （常见于另一个 dsh 实例正开着）——关掉它，或以管理员重试；")
            typer.echo("  3) 端口被占：关掉其它 dsh 实例。")
            typer.echo("  Web 面板与 MCP 通道仍在后台运行：http://127.0.0.1:8080")
    except KeyboardInterrupt:
        typer.echo("\n[launcher] 收到 Ctrl+C，收尾…")
    finally:
        _cleanup()


@app.command("dsh-config")
def dsh_config() -> None:
    """只读诊断：打印 dsh **组合后**配置（验证隔离 overlay 是否生效，《并存原理》§9.4①）。

    期望看到：`paperpilot` 条目在列；webserver 行带 `!!js ctx.webStartup.port` 表达式；
    `remote-web-ui` 行带 `disabled: true`（该条是**本实例的**威胁：钉端口 + 回写共享 profile）。
    不起服务、不改任何文件。**patch 缺一份就响亮失败**——否则"没传"与"传了没生效"
    在 dump 里不可区分（前者曾把 No plugin 伪装成正常启动）。
    """
    dsh_exe = shutil.which("dsh")
    if dsh_exe is None:
        typer.echo("⚠ PATH 里没有 dsh——先 npm i -g @deepseek-ai/dsh")
        raise typer.Exit(code=1)
    patches = _resolve_dsh_patches()
    args = "web" + "".join(f' --patch "{p}"' for p in patches) + " --dump-config"
    rc = subprocess.run(f'"{dsh_exe}" {args}', shell=True).returncode
    raise typer.Exit(code=rc)


def _wait_forever() -> None:
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


@app.command()
def backup(config: Path = typer.Option(None, "--config", "-c")) -> None:
    """把 data/ 打成 zip 备份到 <项目根>/backups/。"""
    from ..config import load_settings

    settings = load_settings(config)
    root = settings.data_dir.parent
    target_dir = root / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"paperpilot-{datetime.now():%Y%m%d-%H%M%S}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in settings.data_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(root))
    typer.echo(f"📦 备份完成: {target}")


@app.command(name="tools")
def tools_cmd(
    config: Path = typer.Option(None, "--config", "-c"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON（供外部程序/AI 解析）"),
) -> None:
    """列出全部能力（中性能力层的自描述清单：名称/说明/入参/读写/可逆）。"""
    from ..capabilities import build_registry
    from ..config import load_settings
    from .container import build_container

    registry = build_registry(build_container(load_settings(config)))
    specs = registry.specs()
    if as_json:
        typer.echo(json.dumps(specs, ensure_ascii=False, indent=2))
        return
    for s in specs:
        kind = "写" if s["kind"] == "write" else "读"
        rev = "·可逆" if s.get("reversible") else ""
        typer.echo(f"[{kind}{rev}] {s['name']} — {s['description']}")
        if s["params"]:
            typer.echo(f"        入参: {', '.join(s['params'])}")


@app.command(name="call")
def call_cmd(
    tool: str = typer.Argument(..., help="能力名（见 paperpilot tools）"),
    param: list[str] = typer.Option(
        None, "--param", "-p", help="key=value，可多次；value 先按 JSON 解析，失败则当字符串"
    ),
    actor: str = typer.Option("cli", "--actor",
                              help="归因：谁在调用。默认 `cli`＝命令行自己（**别借 human 身份**）"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """调用一个能力，打印统一信封 JSON（供任意外部程序/AI 经 subprocess 消费）。

    ⚠ **这是一条"开着"的通道**：它**直调能力注册表** ⇒ **不起 mecha 栈、不走写权门、
    也没有框架操作审计**。所以两条纪律：
    ① 归因默认记 `cli`（**不冒充 human**——本仓"别借身份"的纪律）；
    ② 目标是**写能力**时**响亮警告**（说清绕过了什么），**不静默**。
    受治理的正式入口：dsh 对话里的 MCP 工具面、或 Web（`/settings` 与论文库按钮）。
    """
    from ..capabilities import build_registry
    from ..config import load_settings
    from .container import build_container

    registry = build_registry(build_container(load_settings(config)))
    spec = registry.get(tool)
    if spec is not None and getattr(spec, "kind", "read") == "write":
        typer.echo(
            f"⚠ 绕过写权门：`{tool}` 是**写能力**，本命令直调能力注册表 ⇒ "
            "不走写权门、且**没有框架操作审计**"
            f"（域 journal 仍会记 actor={actor!r}）。"
            "受治理的入口：dsh 对话里的同名工具、或 Web 界面。",
            err=True)
    params = _coerce_params(spec, param or [])
    if spec is not None and "actor" in spec.params:
        params.setdefault("actor", actor)
    result = registry.invoke(tool, **params)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("ok", False):
        raise typer.Exit(code=1)


def _coerce_params(spec, items: list[str]) -> dict:
    """按能力声明的入参类型把 CLI 的 key=value 转成正确类型。

    关键：string 型参数**不做 JSON 解析**——否则 arxiv_id「1706.03762」会被当成浮点数。
    """
    schema = (getattr(spec, "params", None) or {}) if spec else {}
    out: dict = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(f"--param 需 key=value 形式：{item!r}")
        key, raw = item.split("=", 1)
        key = key.strip()
        out[key] = _coerce_value(raw, schema.get(key, {}).get("type", "string"))
    return out


def _coerce_value(raw: str, kind: str):
    try:
        if kind == "integer":
            return int(raw)
        if kind == "number":
            return float(raw)
        if kind == "boolean":
            return raw.strip().lower() in ("1", "true", "yes", "y", "on")
        if kind in ("array", "object"):
            return json.loads(raw)
    except ValueError as exc:
        raise typer.BadParameter(f"参数值 {raw!r} 不是合法的 {kind}") from exc
    return raw  # string：原样保留


def _write_briefing_file(settings, date_str: str, markdown: str) -> None:
    out_dir = settings.data_dir / "briefings"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{date_str}.md").write_text(markdown, encoding="utf-8")


@app.command("topics")
def topics_cmd(
    action: str = typer.Argument("list", help="list | add | update（delete 在 Web 设置页）"),
    name: str = typer.Option("", "--name", help="主题名（add/update 必填）"),
    keywords: str = typer.Option("", "--keywords", help="关键词，逗号分隔"),
    categories: str = typer.Option("", "--categories", help="arXiv 分类，逗号分隔"),
    exclude_keywords: str = typer.Option("", "--exclude-keywords"),
    authors: str = typer.Option("", "--authors", help="关注作者（仅 update）"),
    description: str = typer.Option("", "--description"),
    quota: int = typer.Option(-1, "--quota"),
    threshold: float = typer.Option(-1.0, "--threshold"),
    json_out: bool = typer.Option(False, "--json", help="输出能力信封原文"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """主题管理（W10）：list/add/update 与 MCP/Web 走**同一能力层**（actor=human，
    YAML 单一事实源）；delete 在 Web 设置页（人类侧管理动作）。"""
    import json as _json

    from ..config import load_settings

    settings_obj = load_settings(config)
    if action == "list":
        if not json_out:                       # 保留旧版人读形态（🟢/⚪️ 一眼看启用）
            for t in settings_obj.topics:
                flag = "🟢" if t.enabled else "⚪️"
                typer.echo(
                    f"{flag} {t.name}（分类 {','.join(t.categories) or '不限'} · "
                    f"关键词 {len(t.keywords)} 个 · quota {t.quota} · 阈值 {t.threshold}）"
                )
            raise typer.Exit(0)
        from .. import capabilities
        from .container import build_container
        res = capabilities.registry_for(build_container(settings_obj)).invoke("list_topics")
        typer.echo(_json.dumps(res, ensure_ascii=False, default=str))
        raise typer.Exit(0 if res.get("ok") else 1)
    if action not in ("add", "update"):
        typer.echo(f"未知 action：{action}（list|add|update）")
        raise typer.Exit(2)
    if not name:
        typer.echo(f"{action} 需要 --name")
        raise typer.Exit(2)
    from .. import capabilities
    from .container import build_container

    reg = capabilities.registry_for(build_container(settings_obj))
    if action == "add":
        res = reg.invoke(
            "add_topic", name=name, keywords=keywords, categories=categories,
            exclude_keywords=exclude_keywords, description=description,
            quota=4 if quota < 1 else quota,
            threshold=0.6 if threshold < 0 else threshold,
            actor="human", reason="CLI topics add")
    else:
        res = reg.invoke(
            "update_topic", name=name, keywords=keywords, categories=categories,
            exclude_keywords=exclude_keywords, authors=authors, description=description,
            quota=quota, threshold=threshold, actor="human", reason="CLI topics update")
    typer.echo(_json.dumps(res, ensure_ascii=False, default=str, indent=None if json_out else 2))
    raise typer.Exit(0 if res.get("ok") else 1)
