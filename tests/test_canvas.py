"""自由画布：元素模型 + Graphviz 布局。

只钉两件事，都是**画出来了但不对**（比崩掉更难发现）：
1. 元素表能把 AI 随手写的形状吃下去（dict/字符串节点、``a->b`` 边、悬空边）；
2. 布局真的给每个元素一套有限坐标，且 ``rankdir=TB`` 时根在上、叶子在下
   （Graphviz 原点在左下，页面在左上——翻转错了整张图会上下颠倒地"看起来正常"）。
"""

from __future__ import annotations

import pytest

from paperpilot.canvas import normalize, to_dot
from paperpilot.graphviz import available, layout

needs_dot = pytest.mark.skipif(not available(), reason="没装 graphviz（vendor/ 里也没有）")

_MIXED = {
    "nodes": [
        {"id": "root", "kind": "badge", "label": "三条路线"},
        {"id": "g1", "kind": "group", "label": "路线一", "fill": "#eef2ff"},
        {"id": "p1", "kind": "paper", "arxiv": "2601.10194", "label": "单光纤 200 原子",
         "parent": "g1"},
        {"id": "n1", "kind": "note", "label": "缺口：\n谁比过？"},
    ],
    "edges": [{"src": "root", "dst": "p1", "label": "代表"},
              {"src": "p1", "dst": "n1", "style": "dotted"}],
}


def test_normalize_tolerates_ai_shapes():
    """dict / 字符串节点、``a->b`` 边、悬空边：都不崩，悬空边丢掉（宁可少一笔，别整图交不上）。"""
    nodes, edges = normalize({
        "nodes": {"a": "标题甲", "b": "标题乙", "b2": "标题丙"},
        "edges": ["a->b", ["b", "b2", "带标签"], {"src": "a", "dst": "不存在的点"}],
    })
    assert {n.id for n in nodes} == {"a", "b", "b2"}
    assert [e.label for e in edges] == ["", "带标签"]
    dup, _ = normalize({"nodes": [{"id": "a"}, {"id": "a"}]})
    assert len({n.id for n in dup}) == 2


@needs_dot
def test_layout_gives_geometry_for_every_element():
    res = layout(to_dot(*normalize(_MIXED)))
    assert res["ok"], res["error"]
    assert set(res["nodes"]) == {"root", "p1", "n1"}          # 框是容器，不占坐标
    for node in res["nodes"].values():
        assert 0 <= node["y"] <= res["height"] + 1
        assert node["w"] > 0 and node["h"] > 0
    assert res["nodes"]["root"]["y"] < res["nodes"]["n1"]["y"]  # rankdir=TB：根在上
    for edge in res["edges"]:                                   # 边给的是可画的贝塞尔路径
        assert edge["d"].startswith("M ") and "C " in edge["d"]
