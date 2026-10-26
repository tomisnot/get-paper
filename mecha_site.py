"""站点声明：告诉 mecha 这份软件怎么被起起来。

**它是数据，不是逻辑**：不起线程、不连网络、不读业务状态。
``python -m mecha --root <本仓>`` 会扫这个文件名并读 ``SITE``。

四个面同属一个进程（`paperpilot serve`）：Web 人面 + MCP 能力面 + cockpit 监控面 +
控制端点。因此只要起这一个 child，人的腿和 AI 的腿就都在。

| 字段 | 依据 |
|---|---|
| `id="GP"` | 暴露给模型的工具名会变成 `gp_<tool>` |
| `ready.port_file=".mcp-port"` | MCP 端点把真实端口写在这里 |
| `ready.probe=.../mcp` + `ok_codes=()` | 该端点没有 2xx 只读路由（`GET /mcp` 回 406），"有 HTTP 响应即算在听" |
| `data_root=仓根` | 运行期状态（库 + 三个端口文件）都在仓内 |
| `monitor.port_file=".cockpit-port"` | cockpit 端点写的监控端口文件 |
| `has_gui=True` | 人面是浏览器里的 Web 工作台 |
| `initial_mode=OPEN` | 这个软件设计中人与 AI 共用写权 |
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mecha.authority import Mode
from mecha.site import DSHSpec, MonitorSpec, ReadySpec, Service, Site

_SITE_ROOT = Path(__file__).resolve().parent
_DATA_DIR = _SITE_ROOT / "data"
_PORT_FILE = ".mcp-port"


def _python() -> str:
    """用本项目 venv 的解释器（包装在这里；框架的 sys.executable 可能是另一个环境）。"""
    venv = _SITE_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv) if venv.is_file() else sys.executable


SITE = Site(
    name="Get Paper",
    services=(
        Service(
            id="GP",
            brief="论文台账 / 兴趣画像 / 草稿",
            argv=(_python(), "-m", "paperpilot", "--root", str(_SITE_ROOT),
                  "serve", "--open"),
            cwd=_SITE_ROOT,
            ready=ReadySpec(port_file=_PORT_FILE,
                            probe="http://127.0.0.1:{port}/mcp",
                            ok_codes=(),
                            timeout_s=90.0),
            data_root=_SITE_ROOT,
            has_gui=True,
            initial_mode=Mode.OPEN,
            how_to_open=("人与 AI 共用写权，通常无需切换。"
                         "要临时独占，在 Web 工作台 /settings 的「写权」卡上切；"
                         "AI 侧拿不到那个地址。"),
            monitor=MonitorSpec(route_path="/paperpilot/monitor-url",
                                port_file=".cockpit-port",
                                title="操作审计"),
            env={"PAPERPILOT_DATA_DIR": str(_DATA_DIR)},
        ),
    ),
    dsh=DSHSpec(
        command=("dsh", "web"),
        cwd=_SITE_ROOT,
        port_preference=3081,
    ),
)

__all__ = ["SITE"]
