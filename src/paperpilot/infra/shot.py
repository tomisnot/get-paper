"""服务端无头截图：用**系统自带的 Chromium**（Edge/Chrome）把页面拍成 PNG。

为什么不用 playwright：本机已经装了 Edge/Chrome（都是 Chromium 内核），`--headless
--screenshot` 就能干同一件事——省掉 ~300MB 依赖，也免了"为了截图再养一个浏览器"。
找不到任何 Chromium 时**响亮报错并给路**（绝不静默产出一张空白图当"证据"）。

全页截图走**两趟**（纯 CLI 拿不到页面高度）：
1. ``--dump-dom "URL?shot=1"``：阅读页在 shot 模式下用一小段脚本把内容高度写进
   ``<html data-pp-height="…">``；我们从 DOM 里读出来；
2. ``--screenshot --window-size=W,H``：按那个高度拍，**不留大片空白**（视觉 token 很贵）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

#: 常见安装位置（本机能跑起来的就是它）。也认 PATH 里的 chrome/chromium。
_CANDIDATES = (
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)
_NAMES = ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome")

#: 截图默认参数：宽度按阅读页布局，最小高度兜底。
DEFAULT_WIDTH = 1440
MIN_HEIGHT = 720
MAX_HEIGHT = 12000
_HEIGHT_RE = re.compile(r'data-pp-height="(\d+)"')
_LAUNCH_TIMEOUT = 90


class ShotError(RuntimeError):
    """截图不可用（没浏览器 / 启动失败 / 没产出文件）。调用方据此给可教学错误。"""


def find_browser() -> str | None:
    for cand in _CANDIDATES:
        if cand and Path(cand).exists():
            return cand
    for name in _NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _base_args(binary: str, width: int, height: int) -> list[str]:
    args = [binary, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", "--no-first-run", "--no-default-browser-check",
            f"--window-size={int(width)},{int(height)}"]
    if os.name != "nt":
        args.append("--no-sandbox")
    return args


def probe_height(binary: str, url: str, *, width: int = DEFAULT_WIDTH,
                 fallback: int = 1600) -> int:
    """第一趟：问页面自己有多高（阅读页在 ``?shot=1`` 时会把高度写进 ``<html>``）。

    ⚠ 必须带 ``--virtual-time-budget``：页面是在 iframe 的 load 回调里（定时器里）写高度的，
    而 ``--dump-dom`` 默认可能在定时器之前就倒 DOM ⇒ 退回固定高度、底部留一大片空白
    （实测踩过）。给它一点虚拟时间，定时器先跑完再倒。
    """
    sep = "&" if "?" in url else "?"
    args = _base_args(binary, width, MIN_HEIGHT) + [
        "--virtual-time-budget=2500",
        "--dump-dom", f"{url}{sep}shot=1",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=_LAUNCH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return fallback
    m = _HEIGHT_RE.search(proc.stdout or "")
    if not m:
        return fallback
    try:
        return max(MIN_HEIGHT, min(MAX_HEIGHT, int(m.group(1)) + 40))
    except ValueError:
        return fallback


def capture(url: str, out_path: Path, *, width: int = DEFAULT_WIDTH,
            height: int = 0, full_page: bool = True, settle_ms: int = 1200) -> dict:
    """把 ``url`` 拍成 PNG 落到 ``out_path``，返回 ``{path,width,height,bytes,browser}``。

    ``full_page``：先 probe 高度再拍（不留空白）；False 则只拍一屏（看某个标记时更省）。
    ``settle_ms``：给字体/KaTeX/图片一点落地时间（`--virtual-time-budget` 是确定性的做法）。
    """
    binary = find_browser()
    if not binary:
        raise ShotError("本机没找到 Edge/Chrome（Chromium 内核）⇒ 服务端截图不可用")
    # ⚠ **必须绝对路径**：Chromium 的 `--screenshot=` 不认相对路径，会以
    # "Failed to write file … 系统找不到指定的路径 (0x3)" 失败（实测踩过）。
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    h = int(height) if height else (probe_height(binary, url, width=width) if full_page
                                    else DEFAULT_WIDTH * 9 // 16)
    h = max(MIN_HEIGHT, min(MAX_HEIGHT, h))
    args = _base_args(binary, width, h) + [
        f"--virtual-time-budget={int(settle_ms)}",
        f"--screenshot={out_path}",
        url,
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=_LAUNCH_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ShotError(f"启动无头浏览器失败：{exc}") from exc
    if not out_path.exists() or out_path.stat().st_size == 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-300:]
        raise ShotError(f"无头浏览器没产出图片（{tail or '无输出'}）")
    return {"path": str(out_path), "width": width, "height": h,
            "bytes": out_path.stat().st_size, "browser": Path(binary).name}
