"""站点声明里那条服务腿的**真子进程冒烟**：声明写的那条 argv 真能起、真会写端口文件。

为什么放在 `tests/`（不是 `check/`）：它**起一个真进程、要等十几秒**，属于普通测试；
`check/` 那三件是**快速确定**的声明契约（`check/test_mecha_site.py`）。

⚠ 两处刻意的偏差（都为了**不碰真状态**）：
1. 用临时目录里的 `settings.yaml`（`data_dir` 指向 tmp）覆盖 `PAPERPILOT_CONFIG` ——
   否则 `build_container` 会往真库写两条启动事件（建表/迁移 + 主题对账）。
   声明里钉的那个值不变，这里只是**测试自己的 env**。
2. 因此端口文件也落在 tmp 里（`<data_dir>/..`）⇒ 与真仓的 `.mcp-port` 互不干扰。

⚠ 探活口径与声明一致：**任意 HTTP 响应即算在听**（实测 `GET /mcp` 回 **406**，见 mecha_site.py）。
不把 406 写死成期望值（那是"抄一个会漂的观测值"）——只要求"拿到了 HTTP 响应"。
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mecha.launch import load_sites

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]


def _service():
    sites = load_sites([REPO])
    for site in sites:
        for svc in site.services:
            if svc.id == "GP":
                return svc
    raise AssertionError("找不到 id=GP 的站点声明")


def test_declared_service_starts_headless_and_writes_port_file(tmp_path):
    svc = _service()
    data_dir = tmp_path / "data"
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(f"data_dir: {data_dir.as_posix()}\n", encoding="utf-8")

    env = {**os.environ, "PAPERPILOT_CONFIG": str(cfg), "PAPERPILOT_DATA_DIR": str(data_dir)}
    port_file = tmp_path / ".mcp-port"
    proc = subprocess.Popen(
        svc.argv, cwd=str(svc.cwd), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 90
        port = 0
        while time.time() < deadline:
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                raise AssertionError(f"服务提前退出 rc={proc.returncode}\n{err[-1500:]}")
            if port_file.is_file():
                raw = port_file.read_text(encoding="utf-8").strip()
                if raw.isdigit() and int(raw) > 0:
                    port = int(raw)
                    break
            time.sleep(0.3)
        assert port, f"90s 内没写出可用的端口文件：{port_file}"

        # 与声明同口径：拿到**任意 HTTP 响应**即证明"HTTP 在听"（实测是 406 ⇒ 用 HTTPError 接）。
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=8) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except OSError as exc:                      # 连不上 = 没在听 ⇒ 红
            raise AssertionError(f"端口文件说 {port}，但 HTTP 连不上：{exc}") from exc
        assert 100 <= code < 600, f"不是 HTTP 响应：{code}"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:       # 收尾不来就强杀（别留孤儿占端口）
                proc.kill()
                proc.wait(timeout=10)
        if proc.stderr is not None:
            proc.stderr.close()
