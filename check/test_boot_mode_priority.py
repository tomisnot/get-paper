"""起步写权模式的三档优先级判据：**argv 显式 > `MECHA_INITIAL_MODE` > 本仓默认**。

## 为什么要有这几条

框架**声明**了"这次启动给什么写权"（`MECHA_INITIAL_MODE`，随 `Service.initial_mode` 一起送），
而**软件必须读它并用来装配闸门** —— 否则"框架声明的"与"软件实际用的"就是**两份真值**。

⚠ 三条不冲突（写在这里免得下一个人以为它们打架）：

| 级 | 谁给的 | 说的是 |
|---|---|---|
| ① `--mode`（argv） | **人**当场敲 | "**这一次**听我的" |
| ② `MECHA_INITIAL_MODE` | **框架 / 启动者** | "**这一次启动**给什么写权" |
| ③ `BOOT_MODE_DEFAULT="open"` | **本项目** | "**没有任何声明时**的出厂策略"（用户裁决"不卡写权"） |

⚠ 判据都跑在**主线程**、不起线程/子进程 ⇒ 异常不会被吞成"崩溃冒充失败"。
"""

from __future__ import annotations

from mecha.authority import Mode

from paperpilot.mecha_adapter.hub import BOOT_MODE_DEFAULT, boot_mode
from tests.conftest import make_settings


def _stack_with_env(tmp_path, env_value: str | None, monkeypatch):
    """按 `_boot_stack` 的同一条装配路起栈：`mode=boot_mode("")`（= 未给 argv）。"""
    from paperpilot.app.container import build_container
    from paperpilot.mecha_adapter.hub import build_stack

    if env_value is None:
        monkeypatch.delenv("MECHA_INITIAL_MODE", raising=False)
    else:
        monkeypatch.setenv("MECHA_INITIAL_MODE", env_value)

    container = build_container(make_settings(tmp_path / "data"))
    return build_stack(container, container.settings.data_dir, "mecha", mode=boot_mode(""))


def test_declared_mode_is_actually_applied(tmp_path, monkeypatch):
    """⭐ ① 框架声明 `human` ⇒ 装配出来的闸门**就是 human**（"声明了要真用上"）。

    ⚠ 为什么断言**装配后的 authority**（而不是只断言 `boot_mode()` 的返回值）：
    只测纯函数的话，"声明了但装配时没用它"照样绿 —— 而那正是本判据要防的分叉。
    红证（实测）：让 `boot_mode` 忽略 env（直接返回 fallback）⇒ 这里得到 OPEN ⇒ 红。
    """
    stack = _stack_with_env(tmp_path, "human", monkeypatch)
    assert stack["authority"].mode is Mode.HUMAN
    # 行为面：人侧此刻能写、AI 侧不能（不是"只改了个字段"）
    stack["authority"].gate("human")
    try:
        stack["authority"].gate("ai")
        raise AssertionError("human 独占时 AI 竟然能写")
    except Exception:                                            # noqa: BLE001 - 被拒即预期
        pass


def test_illegal_declared_mode_falls_back_and_speaks(tmp_path, monkeypatch, capsys):
    """⭐ ② 非法值（`'Mode.AI'` 那种形状）⇒ **回落本仓默认 + 出声**（不许静默当成 ai）。

    ⚠ `str(Mode.AI)` 给的是 `'Mode.AI'` **不是** `'ai'`（今天坑过三个代理 + 一家判据）。
    红证（实测）：把 `initial_mode_from_env` 换成"自己 silent 解析"（不打印）⇒ 本判据红。
    """
    stack = _stack_with_env(tmp_path, "Mode.AI", monkeypatch)
    assert stack["authority"].mode is Mode.OPEN, "非法声明竟被当成有效值"
    out = capsys.readouterr().out
    assert "MECHA_INITIAL_MODE" in out and "Mode.AI" in out, f"没出声（静默回落）：{out!r}"


def test_priority_argv_beats_env_beats_default(monkeypatch):
    """⭐ ③ 三档优先级：**argv 显式 > env > 默认**（各一条，缺一不可）。"""
    monkeypatch.delenv("MECHA_INITIAL_MODE", raising=False)
    assert boot_mode("") is Mode.OPEN                   # ③ 默认

    monkeypatch.setenv("MECHA_INITIAL_MODE", "human")
    assert boot_mode("") is Mode.HUMAN                  # ② env 胜过默认
    assert boot_mode("locked") is Mode.LOCKED           # ① argv 胜过 env

    # 反面：argv 非法**照旧响亮抛**（不许因为"有 env 兜底"就把它静默掉）
    import pytest

    with pytest.raises(ValueError):
        boot_mode("root")


def test_without_env_still_uses_repo_default(tmp_path, monkeypatch):
    """⚠ ④ 没有该 env ⇒ 仍是本仓既有默认（`open`，"不卡写权"）。

    红证（实测）：把 fallback 写死成 `Mode.AI` ⇒ 本判据红。
    """
    assert BOOT_MODE_DEFAULT == "open"
    stack = _stack_with_env(tmp_path, None, monkeypatch)
    assert stack["authority"].mode is Mode.OPEN
    # 空串 = **显式取消**该变量（框架口径）⇒ 同样落到本仓默认
    stack2 = _stack_with_env(tmp_path / "b", "", monkeypatch)
    assert stack2["authority"].mode is Mode.OPEN
