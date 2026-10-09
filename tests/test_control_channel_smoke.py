"""控制端点（`.control-port`）的**真端点冒烟**：能起、端口回读对、三个操作经 wire 真能调。

⚠ 这是"判据 ①"那一条 —— 纯注册表级的判据（`check/test_control_channel.py`）**证不了**
"这条端点真的起得来、`.control-port` 真的写得对"。故这里起**真端点**、用**真 MCP 客户端**调。

⚠ 冒烟用**临时数据根**（`tmp_path`），不碰真库/真端口文件。
"""

from __future__ import annotations

import asyncio
import json

from mecha.authority import Mode
from mecha.software_control import CONTROL_PORT_FILE

from paperpilot.mecha_adapter.control import start_control
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
    endpoint, port = start_control(stack["authority"], gate=stack["gate"],
                                   port_file=str(port_file), log=None)
    try:
        # ① 端口文件写得对（回读的是**真端口**，不是预选值）
        assert port_file.is_file(), f"没写 {CONTROL_PORT_FILE}"
        assert int(port_file.read_text(encoding="utf-8").strip()) == port and port > 0

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
        # ⑤ 开手动面：GP 的人面（Web 工作台）**一直开着** ⇒ **幂等成功**在 payload 里。
        #    ⚠ **本断言改过**（2026-10-09）：原先它断言 `no_manual_surface` 响亮失败 ——
        #    那是把"**常开**"报成"**失败**"（面板会红一次"窗口没开成"，而人面早就在）。
        #    ⇒ 设计 §7.1.1 已补"三种结局"：**失败契约只属于真的没有人面者**。
        #    ⚠ 形状照旧断言 **payload**（框架把 `ok:false` 的失败信封当正文回、不设协议级 isError）。
        open_out = json.loads(got["open_manual_surface"]["text"])
        assert open_out["ok"] is True, f"人面常开却被报成失败：{open_out}"
        assert open_out["status"] == "already-open", open_out
        # ⑥ 且**落账**（§4.3：控制操作也要在同一本账里查得到）
        ops = [e.op for e in stack["history"].events()]
        assert "control.open_manual_surface" in ops, ops
    finally:
        endpoint.stop()
