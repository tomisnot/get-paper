"""Web 面只测**结构**，不测"页面上该显示哪句话"。

判断标准：这条断言挂了，是**页面结构坏了**（路由不通、写绕过门），还是**文案/布局换了**？
只有前者值得留。后者是监控前端具体事务，改一次文案就要改一次测试，成本远大于收益。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from paperpilot.web import create_app

PAGES = ("/", "/network", "/profile", "/dashboard", "/settings")


@pytest.fixture
def client(stack):
    return TestClient(create_app(stack))


def make_linear(ctx, ids, title="测试稿"):
    entries = [{"arxiv_id": aid, "why": "因为对口"} for aid in ids]
    return ctx.repo.save_draft(kind="linear", title=title, note="",
                               payload={"entries": entries})


def test_every_page_renders(client):
    """五个展示/控制页都能出 200——空库也不许炸。"""
    for path in PAGES:
        assert client.get(path).status_code == 200, path


def test_page_writes_go_through_command_face(ctx, client, paper):
    """**结构**：页面上的写必须经命令面 ⇒ 一定有 `command.<name>` 审计事件。

    这是"写只有一条路"的可观测证据；比断言按钮上写了什么字稳得多。
    """
    aid = paper("2610.07002")
    make_linear(ctx, [aid])
    client.post(f"/papers/{aid}/star", data={"next_url": "/"})
    audited = [event["op"] for event in ctx.repo.events(op_prefix="command.")]
    assert "command.star_paper" in audited
    assert ctx.repo.paper_view(aid)["reading"]["starred"] is True


def test_outbound_and_download_leave_signals(ctx, client, paper):
    """**关键结果**：点原文/PDF 必须留下 outbound / download 信号（权最重的两个）。

    直接给 arxiv.org 超链接会绕过系统、把这两个信号永久饿死——这条断言守的就是它。
    """
    aid = paper("2610.07003")
    for path, signal in (("go", "outbound"), ("pdf", "download")):
        response = client.get(f"/papers/{aid}/{path}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("https://arxiv.org/")
        ops = [event["op"] for event in ctx.repo.events(op_prefix="signal:")]
        assert f"signal:{signal}" in ops


def test_settings_config_writes_through_gate(ctx, client, stack):
    """**结构**：设置页的配置写必须落到 Gate 快照（而不是只改进程内变量）。"""
    client.post("/settings/config", data={"half_life_days": "20", "default_regions_n": "4",
                                          "max_pages_per_day": "3", "max_requests": "60",
                                          "recent_draft_days": "5"})
    assert stack.gate.snapshot["half_life_days"] == 20.0
