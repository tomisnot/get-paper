"""人工控制口令的判据（`POST /monitor/mode` 的鉴权件）。

三条：
1. **口令文件必须在仓外**（`~/.paperpilot/control-token`）——这是"AI 不能自授权"这条
   边界**尽量成立**的前提；搬回项目根 ⇒ 判据必红（否则它就是"看起来在防、实际防不住"）。
2. **fail-closed**：未配置 / 空 / 错 ⇒ `verify_control_token` 一律 `False`（**不静默放行**）。
3. **每次发布换新**（R11：观测量必须能变）+ 定时安全比较（不按字符给时序缝）。
"""

from __future__ import annotations

from pathlib import Path

from paperpilot.app import cli
from paperpilot.app.control_token import (
    CONTROL_TOKEN_DIR,
    CONTROL_TOKEN_FILE,
    control_token_path,
    publish_control_token,
    read_control_token,
    verify_control_token,
)

# ---------------------------------------------------------------- ① 仓外（边界）

def test_token_path_is_outside_the_repo():
    """⭐ 口令文件**不许在项目根**（在的话"AI 不能自授权"物理上就不成立）。

    R7 红证：把 `control_token_path()` 改成 `PROJECT_ROOT / …` ⇒ 本判据红。
    """
    token_path = control_token_path().resolve()
    repo = cli.PROJECT_ROOT.resolve()
    assert repo not in token_path.parents, (
        f"控制口令落在仓内：{token_path}（仓根 {repo}）——"
        "AI 能读到它 ⇒ `actor=human` 那道钉死会被绕过成"
        "「AI 的写被记成人的写」。它必须留在仓外。")
    assert token_path.name == CONTROL_TOKEN_FILE
    assert token_path.parent.name == CONTROL_TOKEN_DIR
    assert token_path.parent.parent == Path.home()


# ---------------------------------------------------------------- ② fail-closed

def test_verify_fails_closed_when_token_missing(tmp_path):
    """没有口令文件 ⇒ 任何输入都 False（**不静默放行**）。"""
    missing = tmp_path / CONTROL_TOKEN_DIR / CONTROL_TOKEN_FILE
    assert read_control_token(missing) is None
    for given in ("", None, "guess", "x" * 43):
        assert verify_control_token(given, missing) is False, f"未配置口令时 {given!r} 竟放行"


def test_verify_rejects_wrong_and_empty(tmp_path):
    token_file = tmp_path / CONTROL_TOKEN_FILE
    real = publish_control_token(token_file)
    assert verify_control_token(real, token_file) is True
    assert verify_control_token(real + "x", token_file) is False   # 近似值也不放行
    assert verify_control_token(real[:-1], token_file) is False    # 截断也不放行
    assert verify_control_token("", token_file) is False
    assert verify_control_token(None, token_file) is False
    assert verify_control_token(" ", token_file) is False


def test_empty_token_file_is_not_a_token(tmp_path):
    """空文件（或只有空白）**不算口令**——否则"删空文件"会变成"无口令放行"。"""
    token_file = tmp_path / CONTROL_TOKEN_FILE
    token_file.write_text("", encoding="utf-8")
    assert read_control_token(token_file) is None
    assert verify_control_token("", token_file) is False
    token_file.write_text("   \n", encoding="utf-8")
    assert read_control_token(token_file) is None
    assert verify_control_token("   ", token_file) is False


# ---------------------------------------------------------------- ③ 每次换新 + 可读

def test_publish_rotates_token(tmp_path):
    """每次发布都换新（旧口令随上次进程失效；R11：值必须能变）。"""
    token_file = tmp_path / CONTROL_TOKEN_FILE
    a = publish_control_token(token_file)
    b = publish_control_token(token_file)
    assert a != b
    assert len(a) >= 32
    assert read_control_token(token_file) == b        # 盘上是新的那个
    assert verify_control_token(a, token_file) is False   # 旧口令已失效
    assert verify_control_token(b, token_file) is True


def test_publish_creates_parent_dir(tmp_path):
    """家目录下那层目录不存在时自动建（首次运行不该失败）。"""
    deep = tmp_path / CONTROL_TOKEN_DIR / CONTROL_TOKEN_FILE
    assert not deep.parent.exists()
    token = publish_control_token(deep)
    assert deep.parent.is_dir()
    assert read_control_token(deep) == token
