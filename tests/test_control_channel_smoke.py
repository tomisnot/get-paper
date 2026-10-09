"""控制端点（`.control-port`）的**真端点冒烟**：能起、端口回读对、三个操作经 wire 真能调。

⚠ 这是"判据 ①"那一条 —— 纯注册表级的判据（`check/test_control_channel.py`）**证不了**
"这条端点真的起得来、`.control-port` 真的写得对"。故这里起**真端点**、用**真 MCP 客户端**调。

⚠ 冒烟用**临时数据根**（`tmp_path`），不碰真库/真端口文件。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mecha.authority import Mode

from paperpilot.mecha_adapter.control import (
    CONTROL_PORT_FILE,
    build_control_registry,
    start_control_endpoint,
)
from tests.test_mecha_adapter import _stack


def _call_over_wire(port: int, names: list[str]) -> dict:
    """真 MCP 客户端连上控制端点，逐个调（返回 name → 文本）。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def _run() -> dict:
        url = f"http://127.0.0.1:{port}/mcp"
        async with streamable_http_client(url) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                listed = {t.name for t in (await sess.list_tools()).tools}
                out: dict = {"listed": listed}
                for name in names:
                    args = {"mode": "human"} if name == "set_write_authority" else {}
                    res = await sess.call_tool(name, args)
                    text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
                    out[name] = {"is_error": bool(getattr(res, "isError", False)), "text": text}
                return out

    return asyncio.run(_run())


def test_control_endpoint_starts_and_three_ops_work(tmp_path):
    _container, stack = _stack(tmp_path, open_ai=False)
    port_file = tmp_path / CONTROL_PORT_FILE
    host = start_control_endpoint(
        build_control_registry(stack["authority"], gate=stack["gate"]),
        port_file=str(port_file), log=None)
    try:
        # ① 端口文件写得对（回读的是**真端口**，不是预选值）
        assert port_file.is_file(), f"没写 {CONTROL_PORT_FILE}"
        port = int(port_file.read_text(encoding="utf-8").strip())
        assert port == host.port and port > 0

        got = _call_over_wire(port, ["read_write_authority", "set_write_authority",
                                     "open_manual_surface"])
        # ② 三个操作都在这条端点上
        assert got["listed"] == {"read_write_authority", "set_write_authority",
                                 "open_manual_surface"}, got["listed"]
        # ③ 读：LOCKED 起步 ⇒ ai_can_write=false（⚠ 实measured：回的是 JSON payload）
        read = json.loads(got["read_write_authority"]["text"])
        assert read["ok"] is True and read["mode"] == "locked"
        assert read["ai_can_write"] is False and read["human_holds"] is False
        # ④ 切：经 wire 切到 human ⇒ 立刻生效（同一对象、同一进程，不需要重连）
        assert json.loads(got["set_write_authority"]["text"])["ok"] is True
        assert stack["authority"].mode is Mode.HUMAN
        # ⑤ 开窗：GP 没有窗口 ⇒ **响亮失败**在 payload 里（⚠ 实测：框架把 `ok:false` 的
        #    失败信封**当正文回**，不设 MCP 协议级 `isError` ⇒ 断言 payload，别断言 isError）
        open_out = json.loads(got["open_manual_surface"]["text"])
        assert open_out["ok"] is False, f"没窗口却回了成功：{open_out}"
        # ⚠ 实测形状：失败信封把领域失败放在 `error.info` 里（不是 `error.kind`）——
        #    `{"is_error": True, "error": {"message":…, "info": {"kind":…, "hint":…}}}`
        assert open_out["error"]["info"]["kind"] == "no_manual_surface", open_out["error"]
    finally:
        host.stop()
