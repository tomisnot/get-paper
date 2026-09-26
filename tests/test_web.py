"""Web 层冒烟测试：容器装配 + 关键路由可访问。"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from paperpilot.app.container import build_container
from paperpilot.app.web import create_app
from paperpilot.infra.arxiv import parse_atom

from .conftest import SAMPLE_XML, make_settings


def _client(settings):
    container = build_container(settings)
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    container.repo.upsert_papers(papers)
    container.pipeline.run(force=True)
    return TestClient(create_app(container))


def test_pages_render(tmp_path):
    settings = make_settings(tmp_path / "data")
    client = _client(settings)

    home = client.get("/")
    assert home.status_code == 200
    assert "arXiv 每日简报" in home.text

    digest = client.get(f"/digest/{date.today().isoformat()}")
    assert digest.status_code == 200
    assert "今日必读" in digest.text or "值得一看" in digest.text

    listing = client.get("/papers")
    assert listing.status_code == 200 and "论文库" in listing.text

    detail = client.get("/papers/2608.01101")
    assert detail.status_code == 200 and "AI 精读" in detail.text

    missing = client.get("/papers/9999.99999")
    assert missing.status_code == 200 and "找不到论文" in missing.text

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200 and "研究主题" in settings_page.text

    # 记录仪页（事件总线的只读投影）
    activity = client.get("/activity")
    assert activity.status_code == 200 and "append-only" in activity.text

    health = client.get("/healthz")
    assert health.status_code == 200 and health.json()["ai_provider"] == "heuristic"


def test_paper_actions_and_notes(tmp_path):
    settings = make_settings(tmp_path / "data")
    client = _client(settings)

    resp = client.post("/papers/2608.01101/read", follow_redirects=False)
    assert resp.status_code == 303

    resp = client.post(
        "/papers/2608.01101/note",
        data={"content": "测试笔记"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    detail = client.get("/papers/2608.01101")
    assert "测试笔记" in detail.text


def test_settings_topic_crud(tmp_path):
    settings = make_settings(tmp_path / "data")
    client = _client(settings)

    resp = client.post(
        "/settings/topics",
        data={
            "action": "add",
            "name": "多模态",
            "keywords": "vision-language, CLIP",
            "categories": "cs.CV",
            "quota": 2,
            "threshold": 0.55,
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    container = client.app.state.container
    assert any(t.name == "多模态" for t in container.settings.topics)
    # 配置已写回 YAML（唯一事实源）
    assert "多模态" in settings.config_path.read_text(encoding="utf-8")
