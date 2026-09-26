"""CLI：paperpilot（无参数 = 启动 Web + 每日调度）。

常用：
  paperpilot fetch [--days 3]   抓取论文入库
  paperpilot run [--force]      立即跑一次每日流水线
  paperpilot demo               离线演示（内置样例，不联网）
  paperpilot web [--port 8080]  只启动 Web
  paperpilot mcp                起 MCP 语义通道（给 DSH 等 harness  attach）
  paperpilot ai                 AI 模式：起 Web+MCP，前台跑 dsh（AI 在 dsh 里驱动）
  paperpilot dsh-config         只读诊断：打印 dsh 组合后配置（验证隔离 overlay）
  paperpilot backup             备份 data/ 为 zip
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import typer

from ..infra.arxiv import ArxivClient, parse_atom

app = typer.Typer(
    help="PaperPilot — arXiv 每日文献情报系统",
    no_args_is_help=False,
    add_completion=False,
)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

DEMO_XML = Path(__file__).resolve().parents[1] / "data" / "sample_arxiv.xml"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DSH_DIR = PROJECT_ROOT / "dsh"


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


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config: Path = typer.Option(None, "--config", "-c", help="配置文件路径"),
) -> None:
    if ctx.invoked_subcommand is None:
        _serve(config)


def _serve(config: Path | None) -> None:
    import uvicorn

    from ..config import load_settings
    from .container import build_container
    from .scheduler import start_scheduler
    from .web import create_app

    settings = load_settings(config)
    container = build_container(settings)
    start_scheduler(container.pipeline, settings)
    typer.echo(f"🌐 http://{settings.web.host}:{settings.web.port}  (AI: {container.ai_provider})")
    uvicorn.run(
        create_app(container), host=settings.web.host, port=settings.web.port, log_level="info"
    )


@app.command()
def fetch(
    days: int = typer.Option(3, "--days", "-d", help="抓取最近 N 天提交的论文"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """抓取 arXiv 新论文入库（遵守 3s 限速）。"""
    from ..config import load_settings
    from ..infra.db import init_db, make_engine, make_session_factory
    from ..infra.fts import PaperIndex
    from ..infra.repo import PaperRepository

    settings = load_settings(config)
    engine = make_engine(settings.db_path)
    init_db(engine)
    repo = PaperRepository(make_session_factory(engine), index=PaperIndex(engine))

    client = ArxivClient(cache_dir=settings.cache_dir / "arxiv")
    since = datetime.utcnow() - timedelta(days=days)
    papers = client.fetch_candidates(
        topics=settings.topics,
        categories=settings.arxiv_categories,
        since=since,
    )
    result = repo.upsert_papers(
        papers, actor="human", reason=f"CLI 抓取最近 {days} 天论文"
    )
    typer.echo(f"抓取 {len(papers)} 篇：新增 {result['new']}，更新 {result['updated']}")


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
    """只启动 Web（不带每日调度）。"""
    import uvicorn

    from ..config import load_settings
    from .container import build_container
    from .web import create_app

    settings = load_settings(config)
    container = build_container(settings)
    typer.echo(f"🌐 http://{host}:{port}  (AI: {container.ai_provider})")
    uvicorn.run(create_app(container), host=host, port=port, log_level="info")


@app.command()
def mcp(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8780, "--port", "-p"),
    stdio: bool = typer.Option(False, "--stdio", help="用 stdio 传输（默认 streamable-http）"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """起 MCP 语义通道（DSH 等 harness 经 .mcp-port 文件 attach；仅 localhost）。"""
    from .. import mcp_server

    argv = ["--host", host, "--port", str(port)]
    if stdio:
        argv.append("--stdio")
    if config:
        argv += ["--config", str(config)]
    raise typer.Exit(code=mcp_server.main(argv))


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
) -> None:
    """AI 模式：后台起 Web(:8080) + MCP 语义通道(写 .mcp-port)，**前台跑 dsh**。

    AI 对话与管理全部复用 DSH（harness）；PaperPilot 只暴露语义面（MCP 工具）
    与一个内嵌面板（dsh 插件）。Ctrl+C 收尾后台服务。

    **并存隔离**（mecha-sdk《DSH-实例并存原理.md》§9）：除插件源 patch 外再挂
    `dsh/cordis.isolate.patch.yml`（进程级最后一层）——禁掉钉端口/回写共享 profile 的
    remote-web-ui 与别项目的 MCP client，并还原 webserver 端口表达式；端口自动从
    3081 选（3080 留给官方 dsh）⇒ 本实例与官方/其他项目的 dsh 互不影响。
    """
    import threading

    import uvicorn

    from ..config import load_settings
    from .container import build_container
    from .web import create_app

    settings = load_settings(config)
    container = build_container(settings)

    if not settings.mcp.enabled:
        typer.echo("⚠ mcp.enabled=false：AI 模式需要 MCP 语义通道，仍继续启动…")

    # 1) 后台：Web 面板（dsh 插件 iframe 它）
    web_app = create_app(container)
    web_server = uvicorn.Server(
        uvicorn.Config(web_app, host=settings.web.host, port=settings.web.port, log_level="warning")
    )
    web_thread = threading.Thread(target=web_server.run, daemon=True)
    web_thread.start()
    typer.echo(f"  Web 面板: http://{settings.web.host}:{settings.web.port}（后台）")

    # 2) 后台：MCP 语义通道（写 .mcp-port；dsh 插件据此发现）
    from .. import mcp_server

    mcp_port = mcp_server.find_free_port(settings.mcp.port)
    mcp_server.write_port_file(mcp_port, container=container)
    mcp_srv, _tools = mcp_server.create_server(container)
    mcp_thread = threading.Thread(
        target=lambda: asyncio.run(
            mcp_server.run_streamable_http(mcp_srv, settings.mcp.host, mcp_port)
        ),
        daemon=True,
    )
    mcp_thread.start()
    typer.echo(f"  MCP 语义通道: http://{settings.mcp.host}:{mcp_port}/mcp（端口文件已写）")

    # 3) 前台：dsh harness（AI 界面 + 📄 PaperPilot 面板都在里面）
    dsh_exe = shutil.which("dsh")
    patch = DSH_DIR / "cordis.source.patch.yml"
    if dsh_exe is None:
        # 没有 dsh 也不空手而归：降级为纯 Web 模式（面板 + 每日调度照常）
        typer.echo("⚠  PATH 里没有 dsh——降级为纯 Web 模式。")
        typer.echo("   想用 AI 对话驱动，请先安装：npm i -g @deepseek-ai/dsh，再运行 paperpilot ai")
        typer.echo(f"   Web 面板: http://{settings.web.host}:{settings.web.port}")
        typer.echo(f"   MCP 语义通道仍在跑（http://{settings.mcp.host}:{mcp_port}/mcp），其它 harness 可 attach。")
        _serve_forever(settings, container)
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

    isolate = DSH_DIR / "cordis.isolate.patch.yml"
    patches = [p for p in (patch, isolate) if p.exists()]
    port = dsh_port or _pick_dsh_port()
    dsh_args = "web" + "".join(f' --patch "{p}"' for p in patches) + f" --port {port}"
    if no_open:
        dsh_args += " --no-open"
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
        rc = subprocess.run(f'"{dsh_exe}" {dsh_args}', shell=True, env=env)
        if rc != 0:
            typer.echo(f"[launcher] ⚠ dsh 退出码 {rc}。常见原因：")
            typer.echo("  1) node_modules 缺依赖：cd dsh && npm install 后重试；")
            typer.echo("  2) EPERM ~/.dsh/profiles/web/cordis.yml：该 profile 被占用/无写权限")
            typer.echo("     （常见于另一个 dsh 实例正开着）——关掉它，或以管理员重试；")
            typer.echo("  3) 端口被占：关掉其它 dsh 实例。")
            typer.echo("  Web 面板与 MCP 通道仍在后台运行：http://127.0.0.1:8080")
    except KeyboardInterrupt:
        typer.echo("\n[launcher] 收到 Ctrl+C，收尾…")


@app.command("dsh-config")
def dsh_config() -> None:
    """只读诊断：打印 dsh **组合后**配置（验证隔离 overlay 是否生效，《并存原理》§9.4①）。

    期望看到：webserver 行带 `!!js ctx.webStartup.port` 表达式；remote-web-ui /
    mcp-energy-level / mcp-re0-mecha 行带 `disabled: true`。不起服务、不改任何文件。
    """
    dsh_exe = shutil.which("dsh")
    if dsh_exe is None:
        typer.echo("⚠ PATH 里没有 dsh——先 npm i -g @deepseek-ai/dsh")
        raise typer.Exit(code=1)
    patches = [DSH_DIR / "cordis.source.patch.yml", DSH_DIR / "cordis.isolate.patch.yml"]
    args = "web" + "".join(f' --patch "{p}"' for p in patches if p.exists()) + " --dump-config"
    rc = subprocess.run(f'"{dsh_exe}" {args}', shell=True).returncode
    raise typer.Exit(code=rc)


def _serve_forever(settings, container) -> None:
    """dsh 缺席时的降级运行：Web + 每日调度（阻塞到 Ctrl+C）。"""
    import uvicorn

    from .scheduler import start_scheduler
    from .web import create_app

    start_scheduler(container.pipeline, settings)
    try:
        uvicorn.run(
            create_app(container),
            host=settings.web.host,
            port=settings.web.port,
            log_level="info",
        )
    except KeyboardInterrupt:
        typer.echo("\n[launcher] 收到 Ctrl+C，收尾…")


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


@app.command(name="topics")
def topics_cmd(config: Path = typer.Option(None, "--config", "-c")) -> None:
    """列出当前配置的研究主题。"""
    from ..config import load_settings

    settings = load_settings(config)
    for t in settings.topics:
        flag = "🟢" if t.enabled else "⚪️"
        typer.echo(
            f"{flag} {t.name}（分类 {','.join(t.categories) or '不限'} · "
            f"关键词 {len(t.keywords)} 个 · quota {t.quota} · 阈值 {t.threshold}）"
        )


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
    actor: str = typer.Option("human", "--actor", help="归因：谁在调用（human/ai/...）"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """调用一个能力，打印统一信封 JSON（供任意外部程序/AI 经 subprocess 消费）。"""
    from ..capabilities import build_registry
    from ..config import load_settings
    from .container import build_container

    registry = build_registry(build_container(load_settings(config)))
    spec = registry.get(tool)
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
