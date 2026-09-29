"""M4 的 Web 配套判据：引文网络页/仪表盘页/详情页同步按钮（展示面，编辑走命令面）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from paperpilot.app.web import create_app

from .conftest import SAMPLE_XML
from check.test_ai_experience import _reg


class _FakeScholar:
    def __init__(self, *a, **k):
        pass

    def references(self, ext_id, *, limit=100):
        return [
            {"citedPaper": {"externalIds": {"ArXiv": "1512.03385"}, "title": "ResNet",
                            "citationCount": 90000}, "isInfluential": True},
            {"citedPaper": {"externalIds": {"ArXiv": "1706.03762"}, "title": "Attention",
                            "citationCount": 80000}, "isInfluential": False},
        ]

    def citations(self, ext_id, *, limit=100):
        return [{"citingPaper": {"externalIds": {"ArXiv": "2609.01111"},
                                 "title": "Downstream Follower",
                                 "citationCount": 3}, "isInfluential": False}]

    def close(self):
        pass


def _seed(tmp_path):
    from paperpilot.infra.arxiv import parse_atom
    _c, reg = _reg(tmp_path)
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c.repo.upsert_papers(papers, actor="human", reason="seed")
    return _c, reg, papers


def test_network_page_shows_ai_research(tmp_path, monkeypatch):
    """能红（显示器定位）：空图响亮指路；**AI 侧** sync_citations 调查完，页面即呈现
    两侧节点+可点链接（在库→管理页，未入库上游→arXiv）；画像分进 tooltip。
    注：人类侧无同步按钮（显示器不抢编辑的手）——若模板里冒出同步表单就该红。"""
    import paperpilot.capabilities.tools as tools_mod
    _c, reg, papers = _seed(tmp_path)
    monkeypatch.setattr(tools_mod, "SemanticScholarClient", _FakeScholar)
    client = TestClient(create_app(_c, None), follow_redirects=True)

    body = client.get("/network").text
    assert "引文图谱还空着" in body                      # 空态响亮指路，不白屏
    assert "同步引用" not in body                        # 人类侧无编辑按钮（图谱编辑是 AI 的活）

    # AI 调查两篇（能力面直调，等价于 dsh 里的 sync_citations）：
    for i in (0, 1):
        r = reg.invoke("sync_citations", arxiv_id=papers[i].arxiv_id, actor="ai",
                       reason="测网络")
        assert r["ok"] and r["edges"] == 2

    body = client.get("/network").text
    assert "<svg" in body and "引文网络" in body
    assert f'href="/graph/go/{papers[0].arxiv_id}"' in body        # 在库节点→站内句柄
    assert 'href="/graph/go/1512.03385"' in body                  # 未入库上游也是句柄，不外跳
    assert "arxiv.org/abs" not in body                            # 图上绝不外跳（G2）
    assert "ResNet" in body and "画像分" in body                   # 富化卡：标题与事实行
    assert "显示器" in body                                        # 页面自报定位
    _c.settings.graph.max_edges = 3                          # 边采样：糊屏防复发
    body3 = client.get("/network").text
    assert "画 3 条" in body3


def test_lab_page_reuses_m4_numbers(tmp_path):
    """能红：仪表盘数字与 coverage/stats_timeseries 同源；缺卡工单列出；无 AI 调用时
    成本卡给"无调用"而不是空转。"""
    _c, reg, papers = _seed(tmp_path)
    reg.invoke("write_summary", arxiv_id=papers[0].arxiv_id, tldr="卡一", reason="测")
    client = TestClient(create_app(_c, None))
    body = client.get("/lab").text
    assert "调研仪表盘" in body and "TL;DR 卡" in body
    assert "缺卡工单" in body
    assert f"/papers/{papers[0].arxiv_id}" not in body      # 有卡的不该再躺在工单里
    assert "AI 成本" in body
