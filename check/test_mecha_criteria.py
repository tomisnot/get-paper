"""Phase 4 · PaperPilot 接入判据（R7–R15：每条守卫配「能红」证据 + 「不许误报」对偶）。

框架的门禁验不了 PaperPilot 的领域正确性——这些判据自己写。六条接入不变式：
① 命名守卫（工具名合 mecha 律，且守卫真能红）；② capabilities↔tools 单一来源
（22 能力全投影、参数机械派生无手抄漂移）；③ 工具可达性（非「列得出调不动」）；
④ 必填项投影不塌；⑤ 命令审计落史；⑥ authority 拒绝可归因。⑤⑥ 已在
test_mecha_adapter 覆盖，此处补 ①②③④ 的能红/对偶。全部确定性，非概率。
"""

from __future__ import annotations

import pytest
from mecha.errors import MechaError
from mecha.providers.mcp import build_mcp_server, resolve_required
from mecha.toolhost import LocalToolHost
from mecha.tools import ToolRegistry, check_tool_name

from paperpilot.capabilities import registry_for
from paperpilot.mecha_adapter.hub import INSTRUCTIONS, MCP_SERVER_NAME
from paperpilot.mecha_adapter.tools import TOOL_DECLS, TOOL_TO_CAPABILITY
from tests.test_mecha_adapter import EXPECTED_TOOLS, _stack


# ---------------------------------------------------------------- ① 命名守卫
def test_tool_names_obey_mecha_naming_law(tmp_path):
    """不许误报：24 个工具名逐个过 mecha 的 check_tool_name（不抛即合规）。"""
    _c, stack = _stack(tmp_path)
    for schema in stack["tools"].schemas():
        check_tool_name(schema["name"])          # 违例会抛 MechaError


def test_naming_guard_can_redden(tmp_path):
    """能红证据：get_/list_ 前缀与项目前缀确被守卫拒——改名不是多此一举。

    ⚠ **语料里不再有"单段名"**（原先是 `undo`）：框架**第 4 批有意放宽**命名律，允许
    **单段天然动词**（`undo` / `reset` / `sync`）⇒ 再把 `undo` 当违例就是**过时期望**
    （**不是判据变松**：它现在合法是设计如此）。本条的**能红性由另外三条保住**：
    `get_*` ×2 / `list_*` ×1 + 项目前缀那条。
    ⚠ 名字是**模型可见面**——放宽 ≠ 要改名，本仓工具名（含 `undo_change`）一律不动。
    """
    for bad in ("get_paper", "list_topics", "get_digest"):
        with pytest.raises(MechaError) as ei:
            check_tool_name(bad)
        assert ei.value.kind == "bad_tool_name"
    # 项目前缀也拒
    with pytest.raises(MechaError):
        check_tool_name("paperpilot_read", banned_prefixes=("paperpilot",))


# ---------------------------------------------------------------- ② 单一来源
def test_every_capability_projected_exactly_once(tmp_path):
    """22 能力全被投影、无幽灵工具：TOOL_TO_CAPABILITY 的值集 == 能力层名单。

    能红：漏投影任一能力 / 多一个不存在的能力，集合比较当场失败（确定性）。
    """
    container, _stack_ = _stack(tmp_path)
    cap_names = set(registry_for(container).names())
    assert len(cap_names) == 58
    # 人类专属写能力（不投影给 AI）：reset_profile＝防自改画像锚；
    # delete_graph_view＝图是作品、处置权归人；delete_mark＝批注是精读痕迹、处置权归人。
    human_only = {"reset_profile", "delete_graph_view", "delete_mark"}
    assert set(TOOL_TO_CAPABILITY.values()) == cap_names - human_only
    assert len(set(TOOL_TO_CAPABILITY)) == len(TOOL_TO_CAPABILITY) == 55


def test_tool_parameters_derive_from_capability_no_drift(tmp_path):
    """工具参数 = 能力自描述 params 机械派生（减 omit），无手抄第二份。

    能红：任一工具的参数键集与其能力的 params（减 omit）不符即失败。
    """
    container, stack = _stack(tmp_path)
    tools = stack["tools"]
    for decl in TOOL_DECLS:
        tool = tools.get(decl.mecha_name)
        cap_params = registry_for(container).get(decl.cap_name).params
        expected = set(cap_params) - set(decl.omit)
        assert set(tool.parameters) == expected, f"{decl.mecha_name} 参数漂移"
        # 写工具的 actor 由通道钉死，绝不对模型暴露
        if decl.kind == "write":
            assert "actor" not in tool.parameters


# ---------------------------------------------------------------- ③ 工具可达性
def test_tools_are_reachable_not_just_listable(tmp_path):
    """能红证据（EL 最贵的一条）：把空注册表当 toolhost → 「列得出调不动」被构造期拦。"""
    _c, stack = _stack(tmp_path)
    # 正路：真注册表构造成功
    srv = build_mcp_server(stack["tools"], server_name=MCP_SERVER_NAME,
                           instructions=INSTRUCTIONS,
                           required_source=stack["required_source"])
    assert srv is not None
    # 能红：绑一个空 toolhost（模拟误传 assemble 的空 sw.tools）→ 当场响亮报错
    empty_host = LocalToolHost(ToolRegistry())
    with pytest.raises(MechaError) as ei:
        build_mcp_server(stack["tools"], server_name=MCP_SERVER_NAME,
                         instructions=INSTRUCTIONS, toolhost=empty_host)
    assert ei.value.kind == "toolhost_registry_mismatch"


# ---------------------------------------------------------------- ④ 必填投影
def test_required_projection_not_hollowed_by_kwargs(tmp_path):
    """execute 用 **kwargs 包装 ⇒ 签名推必填得空集；注入来源必须补回权威必填项。

    能红：若 RequiredSource 缺失/错误，resolve_required 会回退成空集，断言失败。
    """
    _c, stack = _stack(tmp_path)
    tools = stack["tools"]
    rs = stack["required_source"]
    cases = {
        "read_paper": {"arxiv_id"},
        "add_note": {"arxiv_id", "content"},
        "submit_review": {"reviews"},        # N6：date 已改默认""（可选），只 reviews 必填
        "add_topic": {"name"},
        "set_topic_enabled": {"name", "enabled"},
    }
    for name, want in cases.items():
        got = set(resolve_required(name, tools.get(name), rs))
        assert got == want, f"{name} 必填投影 = {got}，应 {want}"
    # 全可选的工具必填集为空（不误报必填）
    assert resolve_required("fetch_papers", tools.get("fetch_papers"), rs) == []


def test_expected_tools_covers_config_and_authority_surface():
    """不许误报：模型可见面 = 55 投影能力 + 非能力面 6 = **61 工具**。

    （三个能力人类专属、刻意不投影：reset_profile / delete_graph_view / delete_mark。）
    """
    from tests.test_mecha_adapter import NON_CAPABILITY_TOOLS
    assert EXPECTED_TOOLS == set(TOOL_TO_CAPABILITY) | NON_CAPABILITY_TOOLS
    assert len(EXPECTED_TOOLS) == 61
