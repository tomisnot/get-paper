"""统一启动入口的判据：端口预检（R7 能红 + 不许误报）+ **dsh overlay patch 真的被传下去**。

两条独立的事，都是"启动静默半残"这一类：

1. **端口预检**：Web 在后台线程跑 uvicorn，端口被占时只在该线程静默死掉、主流程却继续拉
   MCP/cockpit/dsh ⇒ 半残态（实测 Errno 10048）。`_ensure_port_free` 启动前预检，占了响亮退出。
2. **patch 传递**（本轮新增，修一个真 bug）：`PROJECT_ROOT` 曾写成 `parents[2]`（本文件在
   `src/paperpilot/app/`，正确是 `parents[3]`）⇒ `DSH_DIR` 指向不存在的 `<项目>/src/dsh`
   ⇒ 两个 `--patch` **被静默过滤掉** ⇒ dsh 起时零 patch ⇒ 插件根本不加载
   ⇒ 用户现象"监控面板打不开 / 会话头部什么都没有"（真根因不在面板）。

⚠ **为什么本文件原先没抓到它（R10 的原话）**：本文件当时**只**覆盖 `_ensure_port_free`
这一条路径，从未触碰 `PROJECT_ROOT` / `DSH_DIR` / patch 解析 ⇒ 判据全绿，而 dsh 实际拿到
**0 个 `--patch`**。**"全绿"只对判据覆盖到的路径成立**——这正是 R10 要防的那种假绿。
"""

from __future__ import annotations

import re
import socket
import time
from pathlib import Path

import pytest
import typer

from paperpilot.app import cli
from paperpilot.app.cli import _ensure_port_free


# ---------------------------------------------------------------- 起步写权模式（本批行为变更）
def test_boot_mode_default_is_open_and_maps_four_values():
    """⭐ GP 以 **OPEN** 起步（"不卡写权"）；四个值都能映射，未知值**响亮拒绝**。

    ⚠ 这是**行为变更**：从前启动默认是 LOCKED（AI 要等人类开闸），现在默认"两侧都能写"；
    `locked` 仍在（维护/急停），`ai`/`human` 也仍在（= 从前的独占形态，能力没丢）。
    """
    from mecha.authority import Mode

    from paperpilot.mecha_adapter import hub

    assert hub.BOOT_MODE_DEFAULT == "open"
    assert hub.boot_mode("open") is Mode.OPEN
    assert hub.boot_mode("locked") is Mode.LOCKED
    assert hub.boot_mode("human") is Mode.HUMAN
    assert hub.boot_mode("ai") is Mode.AI
    assert hub.boot_mode("") is Mode.OPEN                 # 空 ⇒ 默认（不猜别的）
    with pytest.raises(ValueError):
        hub.boot_mode("root")                             # 未知值不静默放过


def test_cli_exposes_mode_flag_and_dropped_open_gate():
    """CLI 契约：`--mode`（四值，默认 open）**取代** `--open-gate`；不留别名（别让名字撒谎）。

    判据直接读**函数签名**（不起服务、不跑 uvicorn）——`serve` / `ai` 是 typer 命令的原函数。
    """
    import inspect

    for fn in (cli.serve, cli.ai):
        params = inspect.signature(fn).parameters
        assert "mode" in params, f"{fn.__name__} 应当有 --mode（起步写权模式）"
        default = params["mode"].default
        assert getattr(default, "default", default) == "open", (
            f"{fn.__name__} 的 --mode 默认应为 open，实际 {default!r}")
        assert "open_gate" not in params, (
            f"{fn.__name__} 不该再有 --open-gate（默认已是 open；那个 flag 的行为是'收成只有 AI 能写'，名字会撒谎）")


def test_hub_arg_parser_has_mode_not_open_gate():
    """`paperpilot-mecha` 那条 CLI 同一条口径：`--mode` 在场、`--open-gate` 已删。"""
    from paperpilot.mecha_adapter import hub

    parser = hub._arg_parser()
    opts = {a for action in parser._actions for a in action.option_strings}
    assert "--mode" in opts, f"应有 --mode：{sorted(opts)}"
    assert "--open-gate" not in opts, f"--open-gate 应已删除：{sorted(opts)}"
    ns = parser.parse_args([])
    assert ns.mode == hub.BOOT_MODE_DEFAULT
    assert parser.parse_args(["--mode", "locked"]).mode == "locked"


def _grab_port() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    return s, s.getsockname()[1]


# ---------------------------------------------------------------- 端口预检

def test_ensure_port_free_reddens_on_occupied():
    """能红：端口被占 → typer.Exit(1)，绝不半启动。"""
    s, port = _grab_port()
    s.listen(1)
    try:
        with pytest.raises(typer.Exit) as ei:
            _ensure_port_free("127.0.0.1", port, "Web ")
        assert ei.value.exit_code == 1
    finally:
        s.close()


def test_ensure_port_free_passes_on_free():
    """不许误报：空闲端口通过预检（不抛）。"""
    s, port = _grab_port()
    s.close()                              # 释放 → 该端口此刻空闲
    _ensure_port_free("127.0.0.1", port, "Web ")


# ---------------------------------------------------------------- dsh 根与 patch 传递

def test_dsh_dir_is_project_root_child():
    """路径 off-by-one 回归守卫：`DSH_DIR` 必须是**真实存在**的 `<项目根>/dsh`。"""
    assert cli.DSH_DIR.is_dir(), (
        f"DSH_DIR 不存在：{cli.DSH_DIR}——`PROJECT_ROOT` 的层数写错了？"
        f"（本文件在 src/paperpilot/app/ ⇒ 项目根是 parents[3]）")
    assert cli.DSH_DIR.name == "dsh"
    for name in ("cordis.source.patch.yml", "cordis.isolate.patch.yml"):
        assert (cli.DSH_DIR / name).is_file(), f"缺 patch：{cli.DSH_DIR / name}"


def test_dsh_dir_guard_can_redden():
    """**能红证据**：少一层（`parents[2]` = `src/`）时 dsh 不存在 ⇒ 上一条守卫真的能红。

    这不是复述实现：它直接算出"写错一层"的那个路径，证明该错误**可被判据看见**
    （修 bug 前实测：`parents[2] = <项目>\\src`，其下无 `dsh`）。
    """
    wrong_root = Path(cli.__file__).resolve().parents[2]
    assert wrong_root != cli.PROJECT_ROOT
    assert not (wrong_root / "dsh").is_dir()


def test_ai_argv_carries_both_patches():
    """R10：真正交给 dsh 的 argv 里必须含**两个** `--patch`，且 `isolate` 在最后一层。"""
    patches = cli._resolve_dsh_patches()
    assert len(patches) == 2, f"patch 数不对（静默少传？）：{patches}"
    argv = cli._dsh_argv(patches, 3081)
    for patch in patches:
        assert f'--patch "{patch}"' in argv
    assert "--port 3081" in argv
    # overlay 顺序：source 在前、isolate 在后（端口表达式由最后一层决定）
    assert argv.index("cordis.source.patch.yml") < argv.index("cordis.isolate.patch.yml")
    # --no-open 只在需要时追加
    assert cli._dsh_argv(patches, 3081, no_open=True).endswith(" --no-open")


def test_missing_patch_fails_loudly(tmp_path, monkeypatch, capsys):
    """**不许静默少传**：patch 缺失时响亮失败（Exit(1) + 报出缺了哪一份）。"""
    monkeypatch.setattr(cli, "DSH_DIR", tmp_path)     # 空目录 ⇒ 两份都缺
    with pytest.raises(typer.Exit) as ei:
        cli._resolve_dsh_patches()
    assert ei.value.exit_code == 1
    err = capsys.readouterr().err
    assert "cordis.source.patch.yml" in err
    assert "cordis.isolate.patch.yml" in err


def test_partial_patch_set_still_fails_loudly(tmp_path, monkeypatch, capsys):
    """坏形状的负例：只存在一份 patch 也不许"将就着传一份"（漏一道 overlay = 半套隔离）。"""
    (tmp_path / "cordis.source.patch.yml").write_text("# only one", encoding="utf-8")
    monkeypatch.setattr(cli, "DSH_DIR", tmp_path)
    with pytest.raises(typer.Exit) as ei:
        cli._resolve_dsh_patches()
    assert ei.value.exit_code == 1
    assert "cordis.isolate.patch.yml" in capsys.readouterr().err


# ---------------------------------------------------------------- Web 端口发现文件（面板取址的**产地**）

def test_web_port_publish_clears_stale_then_writes(tmp_path):
    """发布 = **先清陈旧、再写自己**（规则来自框架 `mecha.portfile`）。

    为什么必须清：`terminate()` / `taskkill /F` 不跑 Python 的 `finally` ⇒ 端口文件会留下
    上一代的死端口；不清的话面板会拿到一个连不上的地址（"假绿"的邻居）。
    """
    port_file = tmp_path / cli.WEB_PORT_FILE
    port_file.write_text("9999", encoding="utf-8")        # 上一代残留
    cli._publish_web_port(port_file, 8123)
    assert port_file.read_text(encoding="utf-8") == "8123"


def test_web_port_unpublish_only_deletes_own(tmp_path):
    """收尾**只删仍是自己写的那个端口**的文件（别误删接管者刚写进去的新端口）。"""
    port_file = tmp_path / cli.WEB_PORT_FILE
    cli._publish_web_port(port_file, 8123)
    cli._unpublish_web_port(port_file, 8123)
    assert not port_file.exists()

    # 交接窗口：文件已被下一代（8180）改写 ⇒ 老进程收尾不许删它
    cli._publish_web_port(port_file, 8180)
    cli._unpublish_web_port(port_file, 8123)
    assert port_file.read_text(encoding="utf-8") == "8180"


def test_web_port_published_only_when_listening(tmp_path):
    """**端口真在听之后**才写发现文件；没人听就**不写**（不许发布死地址）。"""
    port_file = tmp_path / cli.WEB_PORT_FILE

    # ① 有人在听 ⇒ 写
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    live_port = srv.getsockname()[1]
    try:
        cli._publish_web_port_when_ready("127.0.0.1", live_port, port_file, timeout=5.0)
        deadline = time.time() + 5
        while time.time() < deadline and not port_file.exists():
            time.sleep(0.05)
        assert port_file.read_text(encoding="utf-8") == str(live_port)
    finally:
        srv.close()

    # ② 没人听 ⇒ 不写（路由会 503 + 可读错误）
    port_file.unlink()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    cli._publish_web_port_when_ready("127.0.0.1", dead_port, port_file, timeout=0.6)
    time.sleep(0.9)
    assert not port_file.exists(), "没人听却发布了端口文件 = 面板拿到死地址（假绿）"


def test_web_port_file_name_matches_dsh_panel_config():
    """**跨语言单一来源**：Python 写的端口文件名 == dsh 插件**注入给面板的** `portFile`。

    两边各写一份的后果就是本轮要修的那个病：**地址漂移 ⇒ 面板空白且零报错**。
    这里**按内容读**那个 `.ts`（**不经 git**——本工程有"gitignore 的文件逃出验收"的教训）。

    ⚠ 2026-10-01（依赖化）：面板改从包 `@mecha/dsh-panel` 取，项目值收进 `src/gp-params.ts`
    （`GP_PANEL`，经 `configurePanel` 注入）⇒ 本守卫跟着改读那份**唯一的项目参数家**。
    """
    ts_path = cli.DSH_DIR / "src" / "gp-params.ts"
    assert ts_path.is_file(), f"找不到 dsh 插件参数家：{ts_path}"
    ts = ts_path.read_text(encoding="utf-8")

    port_file = re.search(r"portFile:\s*'([^']*)'", ts)
    assert port_file, "gp-params.ts 里找不到 portFile 的字符串值（形状变了 ⇒ 本守卫要跟着改）"
    assert port_file.group(1) == cli.WEB_PORT_FILE, (
        f"端口文件名漂移：dsh 侧 {port_file.group(1)!r} != Python 侧 {cli.WEB_PORT_FILE!r}")

    route = re.search(r"routePath:\s*'([^']*)'", ts)
    assert route, "gp-params.ts 里找不到 routePath 的字符串值"
    assert route.group(1).startswith("/") and len(route.group(1)) > 1, (
        f"routePath 必须是非空绝对路径：{route.group(1)!r}")


# ---------------------------------------------------------------- 本批两件 CLI 欠账
def _tmp_config(tmp_path):
    """写一份指向 tmp 的 settings.yaml，返回路径（CLI 命令用它，不碰真库/真家目录）。"""
    from paperpilot.config import dump_settings

    from .conftest import make_settings

    s = make_settings(tmp_path / "data")
    path = tmp_path / "settings.yaml"
    path.write_text(dump_settings(s), encoding="utf-8")
    return path


def test_cli_call_actor_is_cli_and_warns_loudly_on_write(tmp_path, capsys):
    """⭐ CLI `call`（欠账 1+7）：归因默认 **`cli`**（不借 human 身份）；**写能力响亮警告**
    （说清"绕过写权门 + 没有框架操作审计"）；**读能力不警告**（对偶，防噪声）。
    """
    import inspect

    from paperpilot.app import cli as cli_mod

    default = inspect.signature(cli_mod.call_cmd).parameters["actor"].default
    assert getattr(default, "default", default) == "cli", "默认归因必须是 cli（不冒充 human）"

    cfg = _tmp_config(tmp_path)
    cli_mod.call_cmd(tool="list_topics", param=[], actor="cli", config=cfg)      # 读能力
    assert "绕过写权门" not in capsys.readouterr().err, "读能力不该打警告（防噪声）"
    # 写能力：**警告必须在**（能力本身可能因参数/数据失败 ⇒ 失败退出是另一回事，这里只要警告）
    with pytest.raises(typer.Exit):
        cli_mod.call_cmd(tool="record_signal",
                         param=["arxiv_id=2608.01101", "signal=view"],
                         actor="cli", config=cfg)
    err = capsys.readouterr().err
    assert "绕过写权门" in err and "没有框架操作审计" in err
    assert "已审计" not in err, "不许在警告里用'已审计'字样（那是谎话）"


def test_cli_fetch_goes_through_the_same_capability(tmp_path, monkeypatch, capsys):
    """⭐ CLI `fetch`（欠账 3）：走**同一个能力** `fetch_papers`（消重），参数与归因都传对。

    从前的病：它自建 `ArxivClient` + 直写 repo ⇒ **命令行与 AI 是两套口径**（去噪/回落/留痕不一致）。
    """
    from paperpilot.app import cli as cli_mod

    seen: dict = {}

    class _FakeRegistry:
        def invoke(self, name, **kw):
            seen["name"], seen["kw"] = name, kw
            return {"ok": True, "fetched": 7, "new": 3, "updated": 4}

    # ⚠ `fetch` 里是**函数内** `from .. import capabilities` ⇒ 补丁要打在**那个模块**上
    #   （打在 `cli_mod` 上没有这个属性——我第一版就这么错了，判据当场红）。
    from paperpilot import capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "registry_for", lambda _c: _FakeRegistry())
    cli_mod.fetch(days=5, config=_tmp_config(tmp_path))
    assert seen["name"] == "fetch_papers", "必须调同一个能力（不许自己实现一套）"
    assert seen["kw"]["days"] == 5 and seen["kw"]["actor"] == "cli", seen["kw"]
    out = capsys.readouterr().out
    assert "新增 3" in out and "更新 4" in out
