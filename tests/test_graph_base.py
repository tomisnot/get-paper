"""图底座判据（docs/GRAPH-FOUNDATION §7 的 G1-G5）：拓扑分层/站内句柄/富化/参数化/域层纯净。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from paperpilot.app.web import create_app
from paperpilot.domain.graph import Edge, Node, label_for, layered_layout

from .test_web_research import _FakeScholar, _seed


# ---------------------------------------------------------------- G1 层数=拓扑
def test_g1_layers_are_topology_and_deterministic():
    ns = [Node(id=c) for c in "abcd"]
    es = [Edge("a", "b"), Edge("b", "c"), Edge("c", "d"), Edge("a", "d")]  # 3 层链+shortcut
    lay = layered_layout(ns, es, sources={"a"})
    layers = {k: v["layer"] for k, v in lay["pos"].items()}
    assert layers == {"a": 0, "b": 1, "c": 2, "d": 1}    # 最短路径深度：shortcut 使 d 升层
    assert lay["layers"] == 3
    again = layered_layout(list(reversed(ns)), list(reversed(es)), sources={"a"})
    assert {k: v["layer"] for k, v in again["pos"].items()} == layers   # 输入序不变性（确定性）


def test_g1b_deep_graph_layers_unbounded():
    chain = [Node(id=f"n{i}") for i in range(9)]
    edges = [Edge(f"n{i}", f"n{i + 1}") for i in range(8)]
    lay = layered_layout(chain, edges, sources={"n0"}, max_nodes=60)
    assert lay["layers"] == 9                            # 图多深画多深，不写死 2/3
    assert lay["pos"]["n8"]["layer"] == 8


# ---------------------------------------------------------------- G4 label 参数
def test_g4_label_respects_config(tmp_path):
    _c, _reg, _p = _seed(tmp_path)
    long_title = "A Very Long Title About Rydberg Quantum Simulation Indeed"
    short = label_for(long_title, "x", max_len=12)
    assert len(short) <= 12 and short.endswith("…")
    assert label_for("Short", "x", max_len=18) == "Short"   # 不误报：未超不截


# ---------------------------------------------------------------- G5 域层纯净
def test_g5_domain_layer_has_no_domain_words():
    src = (Path(__file__).resolve().parents[1] / "src" / "paperpilot"
           / "domain" / "graph.py").read_text(encoding="utf-8")
    for word in ("citation", "paper", "citationedge", "领域词源"):
        assert word not in src.lower(), word


# ---------------------------------------------------------------- G2/G3 web 端
def test_g2_node_handle_never_leaves_site(tmp_path, monkeypatch):
    """能红（F3）：未入库节点点击 → 走 fetch_paper_by_id 入库 → 进本站管理页；
    全程不外跳、事件留痕；arXiv 挂了也响亮回图谱不白屏。"""
    import paperpilot.capabilities.tools as tools_mod
    _c, reg, papers = _seed(tmp_path)
    monkeypatch.setattr(tools_mod, "SemanticScholarClient", _FakeScholar)
    monkeypatch.setattr(tools_mod, "ArxivClient",
                        lambda *a, **k: _FakeArxivFor(papers))   # 能力要的是构造器，不是实例
    client = TestClient(create_app(_c, None), follow_redirects=True)
    for i in (0, 1):
        assert reg.invoke("sync_citations", arxiv_id=papers[i].arxiv_id)["ok"]

    target = "1512.03385"                                  # 上游、未入库
    assert _c.repo.get_paper(target) is None
    body = client.get(f"/graph/go/{target}").text          # 点击句柄
    assert _c.repo.get_paper(target) is not None           # 自动入库
    assert target in body                                  # 落在管理页（详情渲染）
    r = _c.repo.events_since(since_seq=0, op="upsert_papers", limit=20)   # 域事件名=upsert_papers，非能力名
    # upsert 事件 target 是批量常量 "papers"；身份在 reason（图路由把 id 写进去了）
    assert any(e["actor"] == "human" and target in e["reason"] for e in r["events"])

    boom = client.get("/graph/go/9999.00001")              # fake 报不在 arXiv
    assert "入库失败" in boom.text                          # 响亮，不白屏不外跳


class _FakeArxivFor:
    """id-aware：认样例里的号，外加造一枚“1512.03385”上游分身（真实场景=arXiv 查得到）；
    查不到的号返空 ⇒ 能力 not_found 响亮路径。"""

    def __init__(self, papers, *a, **k):
        import dataclasses
        twin = dataclasses.replace(papers[0], arxiv_id="1512.03385",
                                   title="ResNet (upstream twin)",
                                   abs_url="https://arxiv.org/abs/1512.03385",
                                   pdf_url="https://arxiv.org/pdf/1512.03385")
        self._by_id = {p.arxiv_id: p for p in papers}
        self._by_id["1512.03385"] = twin

    def fetch_by_ids(self, ids):
        return [self._by_id[i] for i in ids if i in self._by_id]

    def close(self):
        pass


def test_g3_hover_card_comes_from_card_system(tmp_path, monkeypatch):
    """能红（F4）：有 summary 的库内节点 hover 卡带五段文字；无卡给补卡指路；
    未入库节点给“点击入库”——图页面自己不生产任何正文。"""
    import paperpilot.capabilities.tools as tools_mod
    _c, reg, papers = _seed(tmp_path)
    monkeypatch.setattr(tools_mod, "SemanticScholarClient", _FakeScholar)
    client = TestClient(create_app(_c, None))
    reg.invoke("sync_citations", arxiv_id=papers[0].arxiv_id)
    body = client.get("/network").text
    assert "还没卡" in body                                 # 库内有篇无卡 ⇒ 指路
    reg.invoke("write_summary", arxiv_id=papers[0].arxiv_id, tldr="图卡可见的一句话",
               reason="测富化")
    body2 = client.get("/network").text
    assert "TL;DR：图卡可见的一句话" in body2               # 五段直进 hover 卡
    assert "点击＝拉进入库并进管理页" in body2              # 未入库节点的降级文案
