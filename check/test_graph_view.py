"""视图面判据（视图一等公民）：编排/回执/默认视图/箭头几何/标签模式/批量钉标。

能红的四类事故（都真实发生过）：
1. **箭头被节点盖住**（圆心→圆心 + marker 随线宽缩）⇒ 这里按几何断言"端点落在圆外"；
2. **分类只在切了着色才存在**（tagmap 条件加载）⇒ 断言默认视图就有图例/角标；
3. **AI 画完没法自查**（无渲染回执）⇒ 断言 /network.json 给出几何与决策；
4. **编排只服从度数**（锚点被截、少数派被挤）⇒ 断言 pin 必留、每组保底、分组成带。
"""

from __future__ import annotations

import math

from fastapi.testclient import TestClient

from paperpilot.app import graph_view as gv
from paperpilot.app.web import create_app
from paperpilot.domain.graph import Edge, Node, layered_layout, timeline_layout
from tests.test_web_research import _FakeScholar, _seed

from .test_graph_base import _FakeArxivFor


def _scholar_patch(monkeypatch):
    import paperpilot.capabilities.tools as tools_mod
    monkeypatch.setattr(tools_mod, "SemanticScholarClient", _FakeScholar)


def test_directional_sides_split_upstream_and_downstream(tmp_path, monkeypatch):
    """上游=它引的（在上）、下游=引用它的（在下）：这是"上下层看不出上下游"的根治。"""
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id)
    reg.invoke("sync_cited_by", arxiv_id=papers[0].arxiv_id)
    payload = gv.build(_c.repo, _c.retrieval, _c.settings,
                       {"root": papers[0].arxiv_id, "depth": 1, "sides": "both"})
    rows: dict[int, list[str]] = {}
    for n in payload["nodes"]:
        rows.setdefault(n["layer"], []).append(n["id"])
    root_row = next(n["layer"] for n in payload["nodes"] if n["is_root"])
    up = {x for k, v in rows.items() if k > root_row for x in v}
    down = {x for k, v in rows.items() if k < root_row for x in v}
    assert "1512.03385" in up and "1706.03762" in up        # 它引的在上
    assert "2609.01111" in down                             # 引用它的在下
    assert any(s["label"].startswith("上游") for s in payload["sides"])
    assert any(s["label"].startswith("下游") for s in payload["sides"])
    up_only = gv.build(_c.repo, _c.retrieval, _c.settings,
                       {"root": papers[0].arxiv_id, "depth": 1, "sides": "upstream"})
    assert all(n["id"] != "2609.01111" for n in up_only["nodes"])
    down_only = gv.build(_c.repo, _c.retrieval, _c.settings,
                         {"root": papers[0].arxiv_id, "depth": 1, "sides": "downstream"})
    assert all(n["id"] != "1512.03385" for n in down_only["nodes"])


def test_directional_sides_reach_the_page(tmp_path, monkeypatch):
    """接线判据：侧标注算出来还得**传进模板**（实测漏过：布局对了但页面没有上游/下游标签）。"""
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id)
    reg.invoke("sync_cited_by", arxiv_id=papers[0].arxiv_id)
    reg.invoke("set_graph_view", name="sd", root=papers[0].arxiv_id, depth=1,
               sides="both", is_default=True, reason="测接线")
    client = TestClient(create_app(_c, None))
    body = client.get("/network").text
    assert body.count('class="gside"') >= 2            # 至少上游/下游两条
    assert "上游 · 它引用的" in body and "下游 · 引用它的" in body
    rec = client.get("/network.json").json()
    assert len(rec["sides"]) >= 2                       # 回执里也要有（自查口径）


def test_materialize_view_brings_every_node_into_library(tmp_path, monkeypatch):
    """图上的每篇都应是库内论文：materialize_view 幂等入库、remaining 可归零。"""
    import paperpilot.capabilities.tools as tools_mod
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    monkeypatch.setattr(tools_mod, "ArxivClient", lambda *a, **k: _FakeArxivFor(papers))
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id)
    reg.invoke("set_graph_view", name="vl", root=papers[0].arxiv_id, depth=1, reason="测")
    before = gv.build(_c.repo, _c.retrieval, _c.settings,
                      {"root": papers[0].arxiv_id, "depth": 1})
    missing0 = [n["id"] for n in before["nodes"] if not n["in_lib"]]
    assert missing0, "种子数据里应有未入库的上游节点"
    r = reg.invoke("materialize_view", name="vl", limit=5, reason="测入库")
    assert r["ok"] and r["fetched"] >= 1
    assert r["remaining"] == max(0, len(missing0) - r["fetched"])
    assert reg.invoke("materialize_view", name="vl", limit=5, reason="测幂等")["ok"]
    assert reg.invoke("materialize_view", name="没有这张", reason="测")["ok"] is False


# ---------------------------------------------------------------- 视图规范
def test_spec_normalization_is_whitelisted_and_clamped():
    s = gv.normalize_spec({"depth": 99, "layout": "nope", "max_edges": 3,
                           "label_mode": "always", "color_by": "tag", "badge": "off"})
    assert s["depth"] == 6                      # 值域收敛
    assert s["layout"] == "layer"               # 枚举回落
    assert s["max_edges"] == 3                  # 下限不误伤小图
    assert s["label_mode"] == "always" and s["color_by"] == "tag"
    assert s["badge"] is False                  # 字符串开关也认


def test_spec_from_query_overrides_view_but_keeps_rest():
    base = gv.normalize_spec({"root": "r1", "depth": 2, "color_by": "tag"})
    out = gv.spec_from_query({"depth": "3"}, base)
    assert out["depth"] == 3 and out["root"] == "r1" and out["color_by"] == "tag"


# ---------------------------------------------------------------- 编排：锚点/分组/年代
def test_explicit_rank_beats_degree_and_centers_pillars():
    """**AI 的判断要压过程序的排序**：rank 里点名的排在前面（哪怕别人被引更高），
    配 center_out 后 rank[0] 落在该行中点——骨干贴住中轴、连线最短，而不是被排到边角。"""
    ns = [Node(id="s")] + [Node(id=f"d{i}") for i in range(5)]
    es = [Edge("s", f"d{i}", weight=float(i)) for i in range(5)]
    plain = layered_layout(ns, es, sources={"s"}, max_nodes=40)
    assert max(plain["pos"], key=lambda k: plain["pos"][k]["x"]) == "d4"      # 默认按度数平铺
    mine = layered_layout(ns, es, sources={"s"}, max_nodes=40,
                          order_map={"d0": 0, "d2": 1}, center_out=True)
    xs = {k: v["x"] for k, v in mine["pos"].items() if k != "s"}
    cx = sum(xs.values()) / len(xs)
    assert abs(xs["d0"] - cx) <= abs(xs["d4"] - cx)        # 我点名的比最高被引的更靠中
    assert abs(xs["d0"] - cx) < abs(xs["d2"] - cx)         # 组内也按我的序号：0 更靠中
    again = layered_layout(list(reversed(ns)), list(reversed(es)), sources={"s"}, max_nodes=40,
                           order_map={"d0": 0, "d2": 1}, center_out=True)
    assert {k: v["x"] for k, v in again["pos"].items() if k != "s"} == xs   # 确定性不变


def test_center_out_reorders_slots_without_moving_the_canvas():
    """`place=center` 只该**重排**行内槽位，不该把点推出画布。

    旧实现（``j if j % 2 else -j``）的偏移幅度随 j 线性增长：12 个点的行被排到
    ``x=-156 … 1544``，而画布宽只有 1288 ⇒ 点跑出框外、边被拽长——用户看到的
    "重要节点居中"反而更挤、更机械。判据钉死三条：**x 集合与 lane 完全相同**、
    **全部落在画布内**、**我点名的第一篇落在该行最靠中轴的槽位**。
    """
    for n in (1, 2, 3, 4, 5, 12, 25):
        ns = [Node(id="s")] + [Node(id=f"d{i}") for i in range(n)]
        es = [Edge("s", f"d{i}", weight=float(i)) for i in range(n)]
        lane = layered_layout(ns, es, sources={"s"}, max_nodes=999)
        ctr = layered_layout(ns, es, sources={"s"}, max_nodes=999, center_out=True,
                             order_map={f"d{i}": i for i in range(n)})
        row_lane = {v["x"] for k, v in lane["pos"].items() if k != "s"}
        row_ctr = {v["x"] for k, v in ctr["pos"].items() if k != "s"}
        assert row_ctr == row_lane, f"n={n}: center 改变了画布几何（应只重排槽位）"
        assert all(0 <= v["x"] <= ctr["width"] for v in ctr["pos"].values()), \
            f"n={n}: 有点被排出画布（宽 {ctr['width']}）"
        cx = sum(row_ctr) / len(row_ctr)
        central = min(sorted(row_ctr), key=lambda x: abs(x - cx))
        assert ctr["pos"]["d0"]["x"] == central, f"n={n}: 骨干没落在最靠中轴的槽位"


def test_align_orders_rows_by_connectivity():
    """**按连接关系排行**：默认按 id/度数平铺，会让"谁引用谁"的两条边交叉横穿画布（实测
    1288px 画布上出现 861px 横跨）——这正是"像程序平铺出来"的观感来源。``sort_within=align``
    把有引用关系的点上下对齐，横跨长度真的变短；**块级**对齐还要保住泳道不碎成碎片。
    """
    def hz(lay):
        return sum(abs(lay["pos"][e.src]["x"] - lay["pos"][e.dst]["x"]) for e in es
                   if e.src in lay["pos"] and e.dst in lay["pos"])

    ns = [Node(id="s1"), Node(id="s2"), Node(id="x"), Node(id="y")]
    es = [Edge("s1", "y"), Edge("s2", "x")]
    plain = layered_layout(ns, es, sources={"s1", "s2"}, max_nodes=99)
    assert plain["pos"]["x"]["x"] < plain["pos"]["y"]["x"]       # 默认平铺：x 在左、y 在右
    al = layered_layout(ns, es, sources={"s1", "s2"}, max_nodes=99, sort_within="align")
    assert al["pos"]["y"]["x"] < al["pos"]["x"]["x"]             # 对齐后 y 落到 s1 下面
    assert hz(al) < hz(plain)                                    # 横跨长度真的变短
    again = layered_layout(list(reversed(ns)), list(reversed(es)), sources={"s1", "s2"},
                           max_nodes=99, sort_within="align")
    assert again["pos"] == al["pos"]                             # 确定性：乱序同输出

    # 块级对齐：同组必须仍然连续，否则分组带（泳道标题）会碎成一片
    ns2 = ([Node(id="s1"), Node(id="s2")]
           + [Node(id=f"a{i}") for i in range(3)] + [Node(id=f"b{i}") for i in range(3)])
    es2 = ([Edge("s1", f"b{i}") for i in range(3)] + [Edge("s2", f"a{i}") for i in range(3)])
    grp = {**{f"a{i}": "A" for i in range(3)}, **{f"b{i}": "B" for i in range(3)}}
    lay2 = layered_layout(ns2, es2, sources={"s1", "s2"}, max_nodes=99,
                          sort_within="align", groups=grp)
    order = sorted((k for k in lay2["pos"] if k[0] in "ab"),
                   key=lambda k: lay2["pos"][k]["x"])
    assert order[0][0] == order[2][0], f"成块的组被打散：{order}"   # 前三同属一组


def test_rank_and_align_compose_instead_of_cancelling():
    """**两把笔各管一段，不互斥**：`rank` 决定"谁贴中轴"，`sort_within=align` 决定"其余怎么排"。

    早先的实现让两者互斥（align 胜、`place` 被收敛掉），结果用户点名要居中展示的骨干
    反而失去中轴位置——"我说了算"被程序改回去。这条判据钉住二者可同时生效。
    """
    ns = [Node(id="s1"), Node(id="s2"), Node(id="p"), Node(id="x"), Node(id="y")]
    es = [Edge("s1", "y"), Edge("s2", "x"), Edge("s1", "p"), Edge("s2", "p")]
    lay = layered_layout(ns, es, sources={"s1", "s2"}, max_nodes=99,
                         sort_within="align", center_out=True, order_map={"p": 0})
    xs = {k: lay["pos"][k]["x"] for k in ("p", "x", "y")}
    cx = sum(xs.values()) / len(xs)
    assert abs(xs["p"] - cx) <= min(abs(xs["x"] - cx), abs(xs["y"] - cx))  # 我点名的贴中轴
    assert lay["pos"]["y"]["x"] < lay["pos"]["x"]["x"]                     # 其余仍按连接对齐


def _three_layer_graph(per_layer=(4, 12, 30)):
    """造一张三层候选数可控的图：s → L1(4) → L2(12) → L3(30)。"""
    ns = [Node(id="s")]
    es = []
    for layer, k in enumerate(per_layer, start=1):
        for i in range(k):
            nid = f"n{layer}_{i}"
            ns.append(Node(id=nid))
            parent = "s" if layer == 1 else f"n{layer - 1}_0"
            es.append(Edge(parent, nid))
    return ns, es


def test_draft_mode_keeps_every_layer_alive_and_within_budget():
    """草稿模式只有一条规则：**每层保底 1**（有候选就不断层），其余按重要性补满预算。

    这一路是"机器替我捞一圈、给我读一眼"，不是交付物；它只需可预测，不需要聪明。
    （历史教训：这里先后用过"均匀 ceil"和"按候选数比例+上限"两版公式，都在替调用方做判断——
    均匀版把每层切成同一个数，比例版让某层独吞预算把库外噪声拉进画面。）
    """
    ns, es = _three_layer_graph(per_layer=(4, 12, 300))     # 极不均衡：某层候选爆炸
    lay = layered_layout(ns, es, sources={"s"}, max_nodes=20)
    seats = {}
    for p in lay["pos"].values():
        seats[p["layer"]] = seats.get(p["layer"], 0) + 1
    assert set(seats) == {0, 1, 2, 3}, f"有层被抹掉：{seats}"    # 每层都活着
    assert min(seats.values()) >= 1
    assert sum(seats.values()) == 20                            # 预算用满
    assert max(seats.values()) <= 20 - 3, f"某层独吞预算：{seats}"


def test_manifest_mode_takes_exactly_what_you_name():
    """**清单模式：机器不加一个点、不减一个点**——点名 2 篇就画 2 篇。

    这是"图画的是我的理解"在域层的落地：选点是我做的，代码只负责把位置摆好。
    """
    ns, es = _three_layer_graph(per_layer=(4, 12, 30))
    lay = layered_layout(ns, es, sources={"s"}, max_nodes=999, layer_budget={3: 2, 1: 1})
    seats = {}
    for p in lay["pos"].values():
        seats[p["layer"]] = seats.get(p["layer"], 0) + 1
    assert seats == {1: 1, 3: 2}, f"没有照单画：{seats}"
    assert len(lay["pos"]) == 3


def test_curated_view_shows_exactly_the_named_papers(tmp_path, monkeypatch):
    """**curated ＝ 我的手**：`layers` 就是内容清单——没点名的绝不出现，点名的绝不被丢。

    这是"图表达的是理解、不是大数据统计"的直接判据：草稿模式会按度数从几百篇候选里替我挑，
    curated 只认名单。写下的顺序也就是层内次序（省得再要一个排序参数）。
    """
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id, actor="ai", reason="测清单")
    client = TestClient(create_app(_c, None), follow_redirects=True)
    root = papers[0].arxiv_id
    d = client.get("/network.json", params={
        "root": root, "mode": "curated",
        "layers": f"{root}:0,1512.03385:-1,1706.03762:-1"}).json()
    ids = {n["id"] for n in d["nodes"]}
    assert ids == {root, "1512.03385", "1706.03762"}, f"清单被篡改：{ids}"
    assert d["stats"]["mode"] == "curated"
    assert d["stats"]["named_missing"] == []
    x = {n["id"]: n["x"] for n in d["nodes"]}
    assert x["1512.03385"] < x["1706.03762"], "写下的顺序没有变成层内次序"


def test_curated_keeps_named_paper_without_edges(tmp_path, monkeypatch):
    """点名的论文**即使一条引用边都没有也要在图上**（"我知道有关系" ≠ "边已经织过"）。

    否则"还没织边但我认定相关"的那种点会被静默丢掉——而它恰恰可能是故事的关键一环。
    """
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id, actor="ai", reason="只织根")
    client = TestClient(create_app(_c, None), follow_redirects=True)
    root, lonely = papers[0].arxiv_id, papers[1].arxiv_id      # lonely 没织过边
    d = client.get("/network.json", params={
        "root": root, "mode": "curated", "layers": f"{root}:0,{lonely}:-2"}).json()
    got = {n["id"]: n for n in d["nodes"]}
    assert lonely in got, f"点名的无边长被丢了：{sorted(got)}"
    assert got[lonely]["in_lib"] is True
    assert got[lonely]["layer"] == 2                            # 清单最大层号 0 ⇒ 行 0−(−2)=2（在最上方）


def test_curated_reports_unknown_names_instead_of_swallowing(tmp_path, monkeypatch):
    """点了一个库里也没有的 id ⇒ **回执里响亮报出来**，不静默吞掉（静默空转＝bug）。"""
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id, actor="ai", reason="测幽灵")
    client = TestClient(create_app(_c, None), follow_redirects=True)
    root = papers[0].arxiv_id
    d = client.get("/network.json", params={
        "root": root, "mode": "curated",
        "layers": f"{root}:0,9999.99999:-1"}).json()
    assert d["stats"]["named_missing"] == ["9999.99999"]
    assert "9999.99999" not in {n["id"] for n in d["nodes"]}


def test_batch_sync_does_many_papers_in_one_call(tmp_path, monkeypatch):
    """**把重复劳动交给代码**：清单里有三十篇就一次织完，不该为三十篇调三十次。

    这是"自由与效率的分工"：选点、分层是判断（交给 AI），"逐篇拉边"是固定重复动作（交给代码）。
    """
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    a, b = papers[0].arxiv_id, papers[1].arxiv_id
    r = reg.invoke("sync_citations", arxiv_id=f"{a},{b}", actor="ai", reason="批量织边")
    assert r["ok"] and r["count"] == 2
    assert {i["arxiv_id"] for i in r["items"]} == {a, b}
    bad = reg.invoke("sync_citations", arxiv_id=f"{a},9999.99999",
                     actor="ai", reason="坏项不伤好项")
    assert bad["ok"] and bad["count"] == 1                      # 好项照织
    assert bad["failed"] == [{"arxiv_id": "9999.99999", "error": "not_found"}]


def test_curated_manifest_is_the_only_source_of_nodes(tmp_path, monkeypatch):
    """**清单只认 `layers`**：从已发布视图继承来的 `pin`/`rank` 不许往清单里加人。

    实测事故：清单写了 16 篇，图上冒出第 17 篇——旧视图的 `pin` 里有一个 id 被当成"点名"。
    这与"点名的才上图"直接冲突，所以 `pin` 只属于草稿模式。
    """
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id, actor="ai", reason="测")
    root = papers[0].arxiv_id
    reg.invoke("set_graph_view", name="旧视图", root=root, depth=1,
               pin=f"{root},1706.03762", reason="造一个带 pin 的基础视图")
    client = TestClient(create_app(_c, None), follow_redirects=True)
    d = client.get("/network.json", params={
        "mode": "curated", "layers": f"{root}:0,1512.03385:-1"}).json()
    assert {n["id"] for n in d["nodes"]} == {root, "1512.03385"}, \
        "继承的 pin 混进了清单"


def test_query_graph_views_pulls_one_out_whole(tmp_path, monkeypatch):
    """**"随时拉出来改"**：给 `name` 就把那一张整幅拉出来（含完整 spec）。

    只给摘要的话，AI 改图就得凭记忆重写整份清单——那正是"作品没法维护"的病根。
    """
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id, actor="ai", reason="测")
    root = papers[0].arxiv_id
    reg.invoke("set_graph_view", name="我的图", mode="curated", root=root,
               layers=f"{root}:0,1512.03385:-1", place="center", reason="测")

    one = reg.invoke("query_graph_views", name="我的图")
    assert one["ok"] and one["spec"]["mode"] == "curated"
    assert one["spec"]["layers"] == {root: 0, "1512.03385": -1}
    assert one["spec"]["place"] == "center" and one["named"] == 2

    lst = reg.invoke("query_graph_views")
    assert [v["name"] for v in lst["views"]] == ["我的图"]
    assert lst["views"][0]["mode"] == "curated" and lst["views"][0]["named"] == 2

    miss = reg.invoke("query_graph_views", name="不存在")
    assert miss["ok"] is False and miss["error"]["kind"] == "not_found"


def test_view_management_works_without_stack(tmp_path, monkeypatch):
    """**无栈形态**（单测/独立部署）下视图管理也要能用：走能力层回退，不白屏、不 500。

    有栈时走命令面（写权门 + 审计），无栈时回退直调——两条路都得通，
    否则"没起 mecha 栈"就等于"图删不掉"。
    """
    _scholar_patch(monkeypatch)
    _c, _reg, _papers = _seed(tmp_path)
    client = TestClient(create_app(_c, None), follow_redirects=True)
    _c.repo.save_graph_view("临时图", {"mode": "curated", "title": "随手一张",
                                      "layers": {"a": 0}},
                            is_default=True, actor="ai", reason="测")
    assert "临时图" in client.get("/settings").text
    assert client.post("/settings/views/delete", data={"name": "临时图"}).status_code == 200
    assert _c.repo.get_graph_view("临时图") is None


def test_rank_and_place_flow_through_the_view_tool(tmp_path, monkeypatch):
    """接线判据：rank/place 必须真的落到 spec（漏参数=被边界层静默吞掉，实测踩过）。"""
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id)
    r = reg.invoke("set_graph_view", name="rk", root=papers[0].arxiv_id, depth=1,
                   rank=f"{papers[0].arxiv_id},1512.03385", place="center", reason="测")
    assert r["ok"] and r["spec"]["rank"] == [papers[0].arxiv_id, "1512.03385"]
    assert r["spec"]["place"] == "center"
    bad = reg.invoke("set_graph_view", name="rk2", place="nope", reason="测收敛")
    assert bad["spec"]["place"] == "lane"                  # 非法枚举收敛而非崩


def test_custom_layers_override_topological_depth(tmp_path, monkeypatch):
    """**按你分析出的逻辑关系分层**：`layers` 点名的论文，层号由 AI 说了算、压过 BFS 跳数。

    这是"只能认拓扑距离 ⇒ 图很机械"的根治：你读过文献，知道某篇其实是**更早的源头**，
    就该能把它钉到上游两层，而不是让程序按"引用跳数"摆在第一层。判据同时钉住
    **生效值回显**（漏参数会被宿主 schema 静默吞掉，只能靠 spec 自查）与**非法输入不崩**。
    """
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    for i in (0, 1):
        reg.invoke("sync_citations", arxiv_id=papers[i].arxiv_id, actor="ai", reason="测分层")
    client = TestClient(create_app(_c, None), follow_redirects=True)
    root = papers[0].arxiv_id

    def layers_of(params):
        d = client.get("/network.json", params=params).json()
        return {n["id"]: n["layer"] for n in d["nodes"]}, d

    plain, _ = layers_of({"root": root, "depth": 2})
    assert plain["1512.03385"] == 1                       # 默认：上游一跳 ⇒ 行 1

    mine, d2 = layers_of({"root": root, "depth": 2, "layers": "1512.03385:-2"})
    assert mine["1512.03385"] == 2                        # 我点名"它其实在两跳之上" ⇒ 更上一层
    assert mine[root] == 0                                # 本体不动
    assert d2["spec"]["layers"] == {"1512.03385": -2}     # 生效值回显（漏参数立刻看得见）

    bad, d3 = layers_of({"root": root, "depth": 2, "layers": "nope:-9,1512.03385:zz"})
    assert bad["1512.03385"] == 1                         # 非整数被丢 ⇒ 该篇仍按拓扑层
    assert bad[root] == 0
    assert d3["spec"]["layers"] == {"nope": -6}           # 幽灵 id 只夹值域、图上无处生效


def test_pin_never_truncated_beyond_budget():
    ns = [Node(id="root")] + [Node(id=f"d{i}") for i in range(30)]
    es = [Edge("root", f"d{i}", weight=float(i)) for i in range(30)]
    lay = layered_layout(ns, es, sources={"root"}, max_nodes=5, pin={"root", "d0"})
    assert "root" in lay["pos"] and "d0" in lay["pos"]      # 锚点在，哪怕预算很小


def test_groups_cluster_in_order_and_emit_bands():
    ns = [Node(id="s")] + [Node(id=f"a{i}") for i in range(3)] + \
         [Node(id=f"b{i}") for i in range(3)]
    es = ([Edge("s", f"a{i}", weight=1.0) for i in range(3)]
          + [Edge("s", f"b{i}", weight=99.0) for i in range(3)])   # 乙组度更高
    groups = {**{f"a{i}": "甲" for i in range(3)}, **{f"b{i}": "乙" for i in range(3)}}
    lay = layered_layout(ns, es, sources={"s"}, max_nodes=40, groups=groups,
                         group_order=["甲", "乙"])
    layer1 = sorted(((p["x"], nid) for nid, p in lay["pos"].items() if p["layer"] == 1))
    # 显式组序优先于度数：甲组在前（否则高被引的乙组会插进来打断聚簇）
    assert [nid for _, nid in layer1][:3] == ["a0", "a1", "a2"]
    bands = lay["bands"]
    assert {b["group"] for b in bands} == {"甲", "乙"} and all(b["n"] == 3 for b in bands)


def test_group_quota_keeps_minority_group():
    """每组保底：总预算只够 few 个时，弱组也不被全灭（防"高被引挤掉少数派"）。"""
    ns = [Node(id="s")] + [Node(id=f"a{i}") for i in range(4)] + [Node(id="b0")]
    es = ([Edge("s", f"a{i}", weight=50.0) for i in range(4)] + [Edge("s", "b0", weight=0.1)])
    groups = {**{f"a{i}": "甲" for i in range(4)}, "b0": "乙"}
    lay = layered_layout(ns, es, sources={"s"}, max_nodes=3, groups=groups,
                         group_quota=1, pin={"s"})
    assert "b0" in lay["pos"]                                # 弱组保底存活


def test_timeline_layout_puts_years_left_to_right():
    ns = [Node(id=x) for x in ("new", "old", "unk")]
    es = [Edge("old", "new"), Edge("old", "unk")]
    lay = timeline_layout(ns, es, sources={"old"},
                          years={"old": 2016, "new": 2024, "unk": 0}, col_gap=100)
    assert lay["pos"]["old"]["x"] < lay["pos"]["new"]["x"]   # 年代升序
    assert any(c["label"] == "未知" for c in lay["columns"])  # 缺年份进"未知"列
    again = timeline_layout(list(reversed(ns)), list(reversed(es)), sources={"old"},
                            years={"old": 2016, "new": 2024, "unk": 0}, col_gap=100)
    assert {k: (v["x"], v["y"]) for k, v in again["pos"].items()} == \
           {k: (v["x"], v["y"]) for k, v in lay["pos"].items()}   # 确定性


# ---------------------------------------------------------------- 页面：默认视图即 AI 发布的那张
def test_default_view_is_what_ai_published(tmp_path, monkeypatch):
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    for i in (0, 1):
        reg.invoke("sync_citations", arxiv_id=papers[i].arxiv_id)
    reg.invoke("tag_paper", arxiv_id=papers[0].arxiv_id, tag="理论源头", reason="测")
    r = reg.invoke("set_graph_view", name="脉络视图", title="五段脉络",
                   root=papers[0].arxiv_id, depth=2, group_by="tag", label_mode="always",
                   reason="测默认视图")
    assert r["ok"] and r["is_default"] and r["url"] == "/network"
    body = TestClient(create_app(_c, None)).get("/network").text
    assert "五段脉络" in body and "单根聚焦" in body            # 首屏＝我发布的那张
    assert "labels-on" in body                                  # 标签常显（label_mode=always）
    assert "理论源头×1" in body and "#7c3aed" in body           # 分类：图例＋配色（无需切设置）
    assert "gbadge" in body                                     # 角标＝文字表达，不只靠颜色
    assert "gband" in body                                      # 泳道分组标题带
    assert "已发布视图" in body and "脉络视图" in body          # 视图切换入口


def test_network_json_is_a_render_receipt(tmp_path, monkeypatch):
    """回执判据：几何/配色/标签决策可自查——**箭头端点必须落在节点圆之外**。"""
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    for i in (0, 1):
        reg.invoke("sync_citations", arxiv_id=papers[i].arxiv_id)
    client = TestClient(create_app(_c, None))
    payload = client.get(f"/network.json?root={papers[0].arxiv_id}&depth=2").json()
    assert payload["stats"]["shown"] > 0 and payload["arrow_size"] >= 6
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert payload["links"], "回执里得有边"
    for lnk in payload["links"]:
        tgt = by_id[lnk["dst"]]
        assert math.hypot(lnk["x2"] - tgt["x"], lnk["y2"] - tgt["y"]) >= tgt["r"] - 0.5, \
            "箭头端点落进了节点圆里（会被节点盖住）"
    colors = {n["fill"] for n in payload["nodes"] if n.get("tag")}
    assert "#7c3aed" in colors or all(c.startswith("#") for c in colors)


def test_batch_tags_and_query_tags(tmp_path, monkeypatch):
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    ids = [p.arxiv_id for p in papers]
    r = reg.invoke("tag_papers",
                   items=f"{ids[0]}:平台源头,{ids[1]}:实验谱系,{ids[2]}:瞎猜",
                   reason="测批量")
    assert r["ok"] and r["count"] == 2 and len(r["skipped"]) == 1     # 坏项不伤好项
    q = reg.invoke("query_tags")
    assert q["ok"] and q["counts"].get("平台源头") == 1
    reg.invoke("tag_papers", items=f"{ids[0]}:动机,{ids[1]}:动机", reason="再测")
    u = reg.invoke("undo", seq=0)                                     # 整批还原
    assert u["ok"] and u["op"] == "set_tags"
    assert reg.invoke("query_tags")["counts"].get("平台源头") == 1


def test_tag_lanes_default_to_causal_order(tmp_path, monkeypatch):
    """泳道顺序是**产品语义**，不该靠调用方每次传对 group_order：
    实测教训——未声明的参数会被工具调用链静默丢掉，视图就退化成字典序（下游扩散排最前）。"""
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    for i in (0, 1):
        reg.invoke("sync_citations", arxiv_id=papers[i].arxiv_id)
    reg.invoke("tag_paper", arxiv_id=papers[0].arxiv_id, tag="实验谱系", reason="测")
    reg.invoke("tag_paper", arxiv_id=papers[1].arxiv_id, tag="动机", reason="测")
    payload = gv.build(_c.repo, _c.retrieval, _c.settings,
                       {"group_by": "tag", "group_quota": 1, "max_nodes": 60})
    assert payload["spec"]["group_order"][:2] == ["动机", "实验谱系"]        # 因果序，非字典序
    assert [lg["tag"] for lg in payload["legend"]] == ["动机", "实验谱系"]    # 图例同序
    lanes = sorted((n["x"], n["group"]) for n in payload["nodes"] if n.get("group"))
    assert [g for _, g in lanes][:2] == ["动机", "实验谱系"]                  # 画面上也按序聚簇


def test_label_auto_threshold_covers_a_typical_view():
    """默认阈值要够到"典型三层视图"（约 75 点）——否则用户又会说"没看到标签"。"""
    assert gv.DEFAULT_SPEC["label_auto_max"] >= 75
    assert gv.normalize_spec({})["label_auto_max"] >= 75


def test_view_publish_is_reversible(tmp_path, monkeypatch):
    _scholar_patch(monkeypatch)
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id)
    reg.invoke("set_graph_view", name="v1", root=papers[0].arxiv_id, reason="一版")
    reg.invoke("set_graph_view", name="v1", root="", layout="timeline", reason="二版")
    assert reg.invoke("query_graph_views")["count"] == 1
    u = reg.invoke("undo", seq=0)                                     # 回到一版
    assert u["ok"] and u["op"] == "set_graph_view"
    spec = _c.repo.get_graph_view("v1")["spec"]
    assert spec["layout"] == "layer" and spec["root"] == papers[0].arxiv_id
    assert reg.invoke("set_default_view", name="不存在")["ok"] is False
