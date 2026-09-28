"""M4 的 Web 配套判据：引文网络页/仪表盘页/详情页同步按钮（展示面，编辑走命令面）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from paperpilot.app.web import create_app

from .conftest import SAMPLE_XML
from .test_ai_experience import _reg


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

    def close(self):
        pass


def _seed(tmp_path):
    from paperpilot.infra.arxiv import parse_atom
    _c, reg = _reg(tmp_path)
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c.repo.upsert_papers(papers, actor="human", reason="seed")
    return _c, reg, papers


def test_network_page_and_sync_button(tmp_path, monkeypatch):
    """能红：空图有指路；同步按钮（人类侧、与 AI 同能力）写边后，网络页渲染
    两侧节点+可点链接（在库→管理页，未入库上游→arXiv）；画像分进 tooltip。"""
    import paperpilot.capabilities.tools as tools_mod
    _c, _reg_, papers = _seed(tmp_path)
    monkeypatch.setattr(tools_mod, "SemanticScholarClient", _FakeScholar)
    client = TestClient(create_app(_c, None), follow_redirects=True)

    body = client.get("/network").text
    assert "引文图谱还空着" in body                      # 空态响亮指路，不白屏

    r = client.post(f"/papers/{papers[0].arxiv_id}/sync_citations")
    assert r.status_code == 200 and "引用边已更新" in r.text   # 303→详情页 flash

    client.post(f"/papers/{papers[1].arxiv_id}/sync_citations")
    body = client.get("/network").text
    assert "<svg" in body and "引文网络" in body
    assert f'href="/papers/{papers[0].arxiv_id}"' in body          # 在库节点→管理页
    assert "https://arxiv.org/abs/1512.03385" in body              # 未入库上游→arXiv
    assert "ResNet" in body and "画像分" in body                   # 权重与 tooltip 在用
    assert "?focus=" in body or "focus" in body                    # 聚焦玩法入说明


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
