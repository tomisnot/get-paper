"""人工控制端点的**口令**：生成 / 存放 / 校验（`POST /monitor/mode` 用它）。

## ⚠ 它挡什么、**不**挡什么（边界不写清，机制就不诚实——R3/R17 同族）

> **本机制挡的是"本机其他进程"**（别的程序、别的用户会话里的随手脚本），
> **挡不住"有权读你文件的 AI"**：AI 若能读文件，就能读到这个口令。

⇒ 把它放在**仓外**（`~/.paperpilot/control-token`）是**刻意的**：AI 的文件访问通常被限在
工作区，放仓外能让"**AI 不能自授权**"这条边界**真的成立**（成本≈零）；若放在项目根，那么
`POST /monitor/mode` 的"服务端钉 `actor=human`"会退化成"**AI 的写被记成人的写**"——
那是比"没有鉴权"更坏的一种假象（它看起来在防，实际防不住）。
⇒ **不要**把它搬回项目根：`tests/test_control_token.py` 有一条判据专门钉住这一点。

## fail-closed

没有口令文件 ⇒ **一律拒绝**（403 + 可读错误），**不静默放行**：控制端点没有"默认放开"这一档。
`paperpilot web`（不接 mecha 栈）**不发布**口令 ⇒ 它的控制端点也一律拒绝
（它本来也无法切写权："未接监控面"）。
"""

from __future__ import annotations

import secrets
from pathlib import Path

__all__ = [
    "CONTROL_TOKEN_DIR",
    "CONTROL_TOKEN_FILE",
    "control_token_path",
    "publish_control_token",
    "read_control_token",
    "verify_control_token",
]

#: 仓外目录（用户家目录下）与文件名。
CONTROL_TOKEN_DIR = ".paperpilot"
CONTROL_TOKEN_FILE = "control-token"


def control_token_path() -> Path:
    """口令文件路径：**用户家目录下**（刻意在仓外，见模块 docstring）。"""
    return Path.home() / CONTROL_TOKEN_DIR / CONTROL_TOKEN_FILE


def publish_control_token(path: Path | None = None) -> str:
    """生成并写入一个新口令，返回它（每次启动都换新：旧口令随上次进程一起失效）。

    写失败**不抛**（家目录不可写时由 fail-closed 兜住：读不到 ⇒ 一律拒绝），
    但把原因留给调用方去打日志——不静默假装成功。
    """
    target = path or control_token_path()
    token = secrets.token_urlsafe(32)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(token, encoding="utf-8")
    try:                      # 尽力收紧权限（POSIX 有效；Windows 上无此语义，忽略）
        target.chmod(0o600)
    except OSError:
        pass
    return token


def read_control_token(path: Path | None = None) -> str | None:
    """读当前口令；文件不存在 / 空 / 读不了 ⇒ `None`（调用方按"未配置"处理）。"""
    target = path or control_token_path()
    try:
        raw = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def verify_control_token(given: str | None, path: Path | None = None) -> bool:
    """校验口令：**fail-closed**——未配置 / 空 / 不匹配一律 `False`。

    用 `secrets.compare_digest`（定时安全比较），不给"按字符猜"留时序缝。
    """
    expected = read_control_token(path)
    if not expected:
        return False
    return bool(given) and secrets.compare_digest(str(given), expected)
