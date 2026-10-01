"""Phase 4 · PaperPilot 接入判据（R7–R15：每条守卫配「能红」证据 + 「不许误报」对偶）。

框架的门禁验不了 PaperPilot 的领域正确性——这些判据自己写。留下的都是**只有本仓能守**的：
① 工具名合 mecha 律（本仓的名字是模型可见面）；② capabilities↔tools 单一来源
（能力全投影、参数机械派生无手抄漂移）；③ 工具可达性（非「列得出调不动」——本仓的栈能造出
一台真 MCP 服务）；④ 必填项投影不塌。
⑤⑥（命令审计落史 / authority 拒绝可归因）已在 `test_mecha_adapter` 覆盖。

⚠ **2026-10-01 删掉三条**（上提/去重，理由逐条见 git 提交说明）：
* 「命名守卫能红」——它测的是**框架自己的** `check_tool_name`（框架单测的份内事）；
* 「空 toolhost 被构造期拦」——同上，`toolhost_registry_mismatch` 是**框架行为**；
* 「模型可见面 = 55+6 = 61」——与 `check/test_capability_map.py` 的 `EXPECTED_TOOLS` 断言**同一事实**。
剩下的每条都答得出"这个承诺只有本仓能守"。
"""

from __future__ import annotations

from mecha.providers.mcp import build_mcp_server, resolve_required
from mecha.tools import check_tool_name

from paperpilot.capabilities import registry_for
from paperpilot.mecha_adapter.hub import INSTRUCTIONS, MCP_SERVER_NAME
from paperpilot.mecha_adapter.tools import TOOL_DECLS, TOOL_TO_CAPABILITY
from tests.test_mecha_adapter import _stack


# ---------------------------------------------------------------- ① 命名守卫
def test_tool_names_obey_mecha_naming_law(tmp_path):
    """不许误报：全部工具名逐个过 mecha 的 `check_tool_name`（不抛即合规）。

    ⚠ 这里**只保留正路**：名字合规是**本仓的数据**（模型可见面），而"守卫本身能红"
    是框架 `check_tool_name` 的性质 —— 那由框架自己的单测守（框架行为不在本仓重复测）。
    """
    _c, stack = _stack(tmp_path)
    for schema in stack["tools"].schemas():
        check_tool_name(schema["name"])          # 违例会抛 MechaError


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
    """本仓的栈能造出一台**真** MCP 服务（不是"列得出调不动"）。

    一句话：**本仓的注册表与端点接得上**（正路）。⚠ 2026-10-01 删掉了对偶那一半
    （"绑空 toolhost ⇒ 构造期 `toolhost_registry_mismatch`"）：那是**框架行为**，
    由框架自己的单测守；本仓只需要证明**自己这条接线是通的**。
    """
    _c, stack = _stack(tmp_path)
    srv = build_mcp_server(stack["tools"], server_name=MCP_SERVER_NAME,
                           instructions=INSTRUCTIONS,
                           required_source=stack["required_source"])
    assert srv is not None
    # 不许误报：这台服务真的带着注册表里的工具（空表也能"造出服务"）
    assert len(stack["tools"].schemas()) > 0


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
