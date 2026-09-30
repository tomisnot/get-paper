"""EXP-4 · 声明==投影 + 信封纪律全表面（两侧都数，别只数一边）。

框架盘点特意点过：只看投影或只看声明都会漏。这里两侧对齐，并把 E1.3 的信封纪律
从"若干调用"扩到"读面 + 安全写面"的全部失败形状（无网络/无权限依赖，确定性）。
"""

from __future__ import annotations

from paperpilot.app.container import build_container
from paperpilot.capabilities import registry_for
from tests.test_mecha_adapter import (
    EXPECTED_TOOLS,
    NON_CAPABILITY_TOOLS,
    TOOL_TO_CAPABILITY,
    _stack,
)

from .conftest import make_settings


def test_declaration_equals_projection(tmp_path):
    """**55 投影能力 + 3 人类专属（不投影）+ 6 非能力面 = 61 投影**。两侧逐字对齐。

    人类专属三名：`reset_profile`（防自改画像锚）、`delete_graph_view`（图是作品、处置权归人）、
    `delete_mark`（批注是精读痕迹、处置权归人）。精读批新增 12 个能力（含跨篇正文检索
    `search_library_text`），投影其中 11 个。
    M4 调研基建批补 sync/upstream/related/coverage/stats/watch 六工具，图底座 P2/P4 批
    补 tag_paper/sync_cited_by，全部投影。

    R2 补 list_briefings/delete_briefing，实用工单补 fetch_paper_by_id/update_topic，
    M0 删 download_paper，M1/M2 画像+feed 批补 get_profile/record_signal/reset_profile/
    feed_generate，feed 期票批补 publish_feed/write_summary（reset 末者**人类专属、
    故意不投影**——守卫显式扣除，不是漏投影）；list_briefings 因命名律改名 query_briefings。
    视图面批补 **set_graph_view/query_graph_views/set_default_view + tag_papers/query_tags**
    ——AI 的画布（发布视图＝/network 首屏、批量钉标、读回标签）。
    """
    _c, stack = _stack(tmp_path)
    projected = {s["name"] for s in stack["tools"].schemas()}
    caps = set(registry_for(stack["container"]).names())

    human_only = {"reset_profile", "delete_graph_view", "delete_mark"}
    assert caps == set(TOOL_TO_CAPABILITY.values()) | human_only
    assert len(caps) == 58
    assert projected == set(TOOL_TO_CAPABILITY) | NON_CAPABILITY_TOOLS   # 投影 = mecha 名 + 非能力面
    assert projected == EXPECTED_TOOLS and len(projected) == 61
    for mecha_name, cap in TOOL_TO_CAPABILITY.items():     # 每个 mecha 名 → 存在的能力
        assert cap in caps, f"{mecha_name} 映射到不存在的能力 {cap}"
        assert mecha_name in projected, f"{mecha_name} 未出现在投影面"


def test_envelope_discipline_all_failure_shapes(tmp_path):
    """信封纪律：各失败 ok:false 必带 error{kind}（无裸 ok:false）——含缺必填的 bad_params。"""
    container = build_container(make_settings(tmp_path / "data"))
    reg = registry_for(container)
    calls = [
        ("get_paper", {"arxiv_id": "0000.00000"}),          # not_found
        ("review_status", {"date": "2000-01-01"}),          # no_review（N2 修后带 error）
        ("undo", {"seq": 9_999_999}),                        # 无可撤销
        ("star_paper", {"arxiv_id": "0000.00000"}),         # not_found
        ("add_note", {"arxiv_id": "0000.00000", "content": "x"}),
        ("set_topic_enabled", {"name": "不存在主题", "enabled": True}),
        ("get_paper", {}),                                   # 缺必填 → bad_params
    ]
    checked = 0
    for name, args in calls:
        env = reg.invoke(name, **args)
        assert env.get("ok") is False, f"{name}{args} 本应失败却 ok=true：{env}"
        err = env.get("error")
        assert isinstance(err, dict) and err.get("kind"), f"裸 ok:false（违反信封纪律）：{name} {env}"
        checked += 1
    assert checked == len(calls)                             # R8：逐个真扫到失败
