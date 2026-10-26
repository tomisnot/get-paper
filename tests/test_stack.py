from __future__ import annotations

import json
import re

import pytest
from mecha.authority import Mode
from mecha.errors import MechaError

from paperpilot.mecha_adapter.commands import _handler_adapter
from paperpilot.mecha_adapter.scope import build_scope_policy
from paperpilot.mecha_adapter.stack import build_stack
from paperpilot.mecha_adapter.tools import build_tools

NAME_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


def names(stack) -> list[str]:
    return [schema["name"] for schema in stack.tools.schemas()]


def test_every_command_is_projected_as_a_tool(stack):
    """**结构**：命令投影成工具，不手抄第二份清单。

    只断言"每条命令都在工具面里"，不数总数——数会把"加一个读工具"变成一次改测试。
    """
    surface = set(names(stack))
    missing = [name for name in stack.command_names if name not in surface]
    assert not missing, f"这些命令没投影成工具：{missing}"
    assert {"read_paper", "read_profile", "query_papers"} <= surface


def test_every_schema_has_description_and_parameters(stack):
    for schema in stack.tools.schemas():
        assert schema["description"].strip()
        assert isinstance(schema["parameters"], dict)
        for param, info in (schema["parameters"] or {}).items():
            assert info.get("type"), f"{schema['name']}.{param} 缺 type"
            # 参数没描述 = 模型只能猜（旧版的血泪教训）
            assert info.get("description"), f"{schema['name']}.{param} 缺 description"


def test_long_lists_are_capped_and_say_so(stack, paper):
    for index in range(30):
        paper(f"2610.030{index:02d}")
        stack.repo.record(actor="ai", op="test_op", target=f"p{index}")
    result = stack.tools.execute("read_activity", {"limit": 50})
    value = result["value"]
    assert len(value["events"]) == 20
    truncated = value["_events_truncated"]
    assert truncated["kept"] == 20
    assert truncated["total"] > 20
    assert truncated["where"]


def test_boot_does_not_rewrite_unchanged_config(ctx, tmp_path):
    """每次启动都 seed 一遍会把事件流淹掉；值没变就不该留痕。"""
    first = build_stack(ctx, root=tmp_path / "boot1")
    before = first.repo.conn.execute(
        "SELECT COUNT(*) c FROM events WHERE actor='bootstrap'").fetchone()["c"]
    first.close()
    second = build_stack(ctx, root=tmp_path / "boot2")
    after = second.repo.conn.execute(
        "SELECT COUNT(*) c FROM events WHERE actor='bootstrap'").fetchone()["c"]
    second.close()
    assert after == before


def test_score_papers_covers_the_whole_window(stack, paper):
    """候选面由时间窗决定，不由 limit 决定——旧写法 `limit*4` 会漏掉窗口里更早的论文。"""
    for index in range(30):
        paper(f"2610.070{index:02d}", published="2026-10-22 00:00:00")
    paper("2610.07099", title="optical tweezer array engineering",
          published="2026-10-19 00:00:00")
    # ⚠ 用显式 windows 而不是 days：days 相对"现在"，判据会随一天里的几点钟红绿跳
    result = stack.tools.execute("score_papers", {"windows": "2026-10-19~2026-10-25",
                                                  "limit": 5})
    value = result["value"]
    assert value["pool"] == 31
    assert len(value["results"]) == 5


def test_score_papers_can_target_a_month_window(stack, paper):
    """历史窗口（比如某个月）评不了就只能靠 days，days 表达不了"1 月"。"""
    paper("2610.09001", title="January paper on optical tweezer array",
          published="2026-01-15 00:00:00")
    paper("2610.09002", title="February paper", published="2026-02-15 00:00:00")
    result = stack.tools.execute("score_papers",
                                 {"windows": "2026-01-01~2026-01-31", "limit": 5})
    value = result["value"]
    assert value["pool"] == 1
    assert value["results"][0]["arxiv_id"] == "2610.09001"


def test_cap_reports_bytes_when_over_budget():
    from paperpilot.mecha_adapter.limits import BUDGET_BYTES, cap
    big = {"papers": [{"t": "x" * 20000} for _ in range(6)]}
    out = cap(big)
    assert len(out["papers"]) == 5
    assert out["_bytes"]["budget"] == BUDGET_BYTES


def test_write_tool_goes_through_gate_and_leaves_operation_id(stack, paper):
    aid = paper("2610.01001")
    result = stack.tools.execute("star_paper", {"arxiv_id": aid})
    assert result["is_error"] is False, result
    assert result["value"]["starred"] is True
    assert stack.repo.paper_view(aid)["reading"]["starred"] is True

    audits = [event for event in stack.repo.events(op_prefix="command.")
              if event["target"] == "" or True]
    assert any(event["op"] == "command.star_paper" for event in audits)
    writes = [event for event in stack.repo.events(op_prefix="star_paper")]
    assert writes and writes[0]["operation_id"]


def test_write_linear_is_incremental_and_returns_checkup(stack, paper):
    """增量语义：两次调用并成一份稿，**顺序＝首次出现的顺序**，改一条只发那一条；
    没提到的条目不动，要删必须写进 remove。"""
    first, second, third = (paper(f"2610.01{n:03d}") for n in (1, 2, 3))
    one = stack.tools.execute("write_linear", {
        "title": "1024", "entries": [{"arxiv_id": first, "why": "对口"},
                                     {"arxiv_id": second, "why": "也看"}]})
    assert one["is_error"] is False, one
    assert one["value"]["checkup"]["no_card"] == [first, second]

    two = stack.tools.execute("write_linear", {
        "title": "1024", "entries": [{"arxiv_id": second, "why": "改过"},
                                     {"arxiv_id": third, "why": "补一篇"}]})
    assert two["is_error"] is False, two
    draft = stack.repo.draft("1024")
    entries = draft["payload"]["entries"]
    assert [e["arxiv_id"] for e in entries] == [first, second, third]   # 新增追加到末尾
    assert entries[1]["why"] == "改过"                                  # 就地更新
    assert two["value"]["checkup"]["elements"] == 3

    stack.tools.execute("write_linear", {"title": "1024", "remove": [second]})
    left = [e["arxiv_id"] for e in stack.repo.draft("1024")["payload"]["entries"]]
    assert left == [first, third]


def test_write_network_merges_nodes_and_fills_paper_titles(stack, paper):
    """网络增量：节点按 id 认人；论文节点只给 arxiv_id，标题由代码从库里补。"""
    aid = paper("2610.01004", title="一篇有标题的论文")
    root = {"id": "root", "kind": "badge", "label": "中心"}
    once = stack.tools.execute("write_network", {
        "title": "谱系", "nodes": [root, {"id": "p1", "kind": "paper", "arxiv_id": aid}]})
    assert once["is_error"] is False, once
    nodes = stack.repo.draft("谱系")["payload"]["nodes"]
    assert nodes[1]["label"] == "一篇有标题的论文"        # 省略 label 也能画
    assert "fill" not in nodes[1]                        # 空字段不落盘

    stack.tools.execute("write_network", {
        "title": "谱系", "nodes": [{"id": "p1", "label": "改个名"}],
        "edges": [{"src": "root", "dst": "p1", "label": "代表"}]})
    payload = stack.repo.draft("谱系")["payload"]
    assert len(payload["nodes"]) == 2                    # 合并，不是重复添加
    assert payload["nodes"][1]["label"] == "改个名"      # 就地更新，arxiv_id 保留
    assert payload["nodes"][1]["arxiv_id"] == aid
    assert payload["edges"] == [{"src": "root", "dst": "p1", "label": "代表"}]

    bad = stack.tools.execute("write_network", {
        "title": "x", "nodes": [{"id": "p", "kind": "paper", "arxiv_id": "2610.99999"}]})
    assert bad["is_error"] is True                       # 不在库就写不进去，不静默丢弃
    assert bad["error"]["info"]["kind"] == "bad_draft"


def test_scope_denied_hides_tool_from_projection(ctx, tmp_path):
    """作用域没授予 ⇒ 该工具**根本不投影**（看不见，省得 AI 去试）。"""
    strict = build_stack(ctx, root=tmp_path / "strict")
    limited = build_scope_policy(allow=("library", "fetch"))
    tools = build_tools(ctx, strict.software, policy=limited, side="ai")
    surface = {schema["name"] for schema in tools.schemas()}
    assert "star_paper" in surface          # library 已授予
    assert "set_config" not in surface      # config 未授予 → 不投影
    assert "write_network" not in surface    # drafts 未授予 → 不投影


def test_authority_blocks_human_write_when_ai_owns(stack, paper):
    """写权检查在**副作用之前**：拒绝时数据库不能变。"""
    aid = paper("2610.01003")
    stack.authority.switch_mode(Mode.AI, side="human")
    result = stack.invoke_human("star_paper", arxiv_id=aid, starred=True)
    assert result["is_error"] is True
    assert stack.repo.paper_view(aid)["reading"]["starred"] is False


def test_read_tool_turns_none_into_default(stack, paper):
    """只读工具同样要扛住"省略参数 → None"（MCP 签名是合成的，没有 Python 默认值）。"""
    paper("2610.02001")
    result = stack.tools.execute("query_papers", {"limit": None, "offset": None})
    assert result["is_error"] is False, result
    assert result["value"]["papers"]
    assert result["value"]["total"] >= 1


def test_none_means_default_for_optional_params():
    """MCP 投影会把没传的可选参数变成 None；适配层必须还原成 handler 的默认值。

    这条是实测撞出来的：`fetch_papers(windows=…)` 走聚合端点时 `regions=None`，
    handler 里 `regions.strip()` 直接炸成 internal_error。
    """
    seen = {}

    def handler(regions: str = "quant-ph", days: int = 3):
        seen.update(regions=regions, days=days)
        return regions

    wrapped = _handler_adapter(handler)
    assert wrapped(context=None, regions=None, days=None) == "quant-ph"
    assert seen == {"regions": "quant-ph", "days": 3}
    assert wrapped(context=None, regions="cond-mat") == "cond-mat"
