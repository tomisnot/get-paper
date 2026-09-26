"""MCP 语义通道活体测试：真 HTTP + 真 MCP 协议（进程内起服务，无子进程）。

覆盖 DSH 插件侧走过的同一条路：streamable-http → initialize → list_tools →
call_tool。工具函数直调测试见 test_mcp_server.py；这里验证「协议层」没裂。

不用 pytest-asyncio（少一个依赖）：每个测试 = 一个 asyncio.run，服务与客户端
在同一个事件循环里起停。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from paperpilot.app.container import build_container
from paperpilot.infra.arxiv import parse_atom
from paperpilot.mcp_server import create_server, find_free_port, run_streamable_http

from .conftest import SAMPLE_XML, make_settings

EXPECTED_TOOLS = {
    "list_topics", "get_digest", "search_papers", "get_paper", "fetch_papers",
    "prepare_review", "submit_review", "finalize_briefing", "run_pipeline",
    "add_topic", "set_topic_enabled", "mark_read", "star_paper", "skip_paper",
    "add_note", "get_activity", "review_status", "undo",
}


async def _wait_port(port: int, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError(f"MCP 服务未就绪: {port}")


async def _start_server(tmp_path):
    """起容器 + 灌样例 + 起 MCP 服务；返回 (url, container, server_task)。"""
    settings = make_settings(tmp_path / "data")
    container = build_container(settings)
    container.repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")))
    srv, _tools = create_server(container)

    port = find_free_port(8791)
    task = asyncio.create_task(run_streamable_http(srv, "127.0.0.1", port))
    await _wait_port(port)
    return f"http://127.0.0.1:{port}/mcp", container, task


async def _stop_server(task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


class _Client:
    """真 MCP 客户端的极简封装（async context manager）。"""

    def __init__(self, url: str) -> None:
        self.url = url

    async def __aenter__(self) -> ClientSession:
        self._ctx = streamable_http_client(self.url)
        read, write = await self._ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self._session

    async def __aexit__(self, *exc) -> None:
        with contextlib.suppress(Exception):
            await self._session.__aexit__(None, None, None)
        with contextlib.suppress(Exception):
            await self._ctx.__aexit__(None, None, None)


def _payload(result) -> dict:
    assert result.content, "MCP 回程应有文本 content"
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------- 测试
def test_live_tools_and_flow(tmp_path):
    async def main() -> None:
        url, _container, task = await _start_server(tmp_path)
        try:
            async with _Client(url) as session:
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert EXPECTED_TOOLS <= names
                prepare = next(t for t in tools.tools if t.name == "prepare_review")
                assert prepare.input_schema.get("type") == "object"

                out = _payload(await session.call_tool("list_topics", {}))
                assert out["ok"] and len(out["topics"]) == 3

                out = _payload(await session.call_tool("search_papers", {"query": "speculative"}))
                assert any(p["arxiv_id"] == "2608.02444" for p in out["papers"])

                # AI 三段流程（经真协议）
                prepared = _payload(await session.call_tool("prepare_review", {}))
                assert prepared["ok"] and prepared["candidates"]
                date_str = prepared["date"]

                reviews = [
                    {
                        "arxiv_id": c["arxiv_id"],
                        "score": 0.93 if i == 0 else 0.25,
                        "label": "must_read" if i == 0 else "skip",
                        "reason": "强相关" if i == 0 else "不相关",
                        **(
                            {"summary": {"tldr": "一句话", "problem": "问题", "method": "方法",
                                         "results": "结果", "novelty": "贡献", "keywords": ["k"]}}
                            if i == 0 else {}
                        ),
                    }
                    for i, c in enumerate(prepared["candidates"])
                ]
                submitted = _payload(
                    await session.call_tool(
                        "submit_review", {"date_str": date_str, "reviews": reviews}
                    )
                )
                assert submitted["ok"]
                assert submitted["accepted"] == len(prepared["candidates"])

                finalized = _payload(
                    await session.call_tool("finalize_briefing", {"date_str": date_str})
                )
                assert finalized["ok"] and finalized["selected"] >= 1

                digest = _payload(
                    await session.call_tool("get_digest", {"date_str": date_str})
                )
                assert digest["exists"]
                assert "arXiv 每日简报" in digest["markdown"]
                assert digest["stats"]["ai_provider"] == "dsh-review"
        finally:
            await _stop_server(task)

    asyncio.run(main())


def test_live_teachable_error(tmp_path):
    """可教学错误经真协议回程（DSH 里 AI 第一次错就能改对）。"""

    async def main() -> None:
        url, _container, task = await _start_server(tmp_path)
        try:
            async with _Client(url) as session:
                payload = _payload(
                    await session.call_tool("get_paper", {"arxiv_id": "0000.00000"})
                )
                assert payload["ok"] is False
                assert payload["error"]["kind"] == "not_found"
                assert payload["error"]["hint"]
        finally:
            await _stop_server(task)

    asyncio.run(main())
