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

import socket
from pathlib import Path

import pytest
import typer

from paperpilot.app import cli
from paperpilot.app.cli import _ensure_port_free


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
