"""arXiv Atom 解析测试。"""

from __future__ import annotations

from paperpilot.infra.arxiv import build_queries, parse_atom, split_version

from .conftest import SAMPLE_XML


def test_split_version():
    assert split_version("http://arxiv.org/abs/2608.01101v2") == ("2608.01101", 2)
    assert split_version("http://arxiv.org/abs/2608.01101") == ("2608.01101", 1)
    assert split_version("http://arxiv.org/abs/math.GT/0309136") == ("math.GT/0309136", 1)


def test_parse_sample_feed(sample_papers):
    papers = sample_papers
    assert len(papers) == 10

    first = papers[0]
    assert first.arxiv_id == "2608.01101"
    assert first.version == 2
    assert "Test-Time Compute" in first.title
    assert "chain-of-thought" in first.abstract.lower()
    assert first.authors[:2] == ["Wei Chen", "Ling Zhao"]
    assert first.primary_category == "cs.CL"
    assert "cs.AI" in first.categories
    assert first.pdf_url.endswith("/pdf/2608.01101v2")
    assert first.abs_url == "https://arxiv.org/abs/2608.01101"
    assert first.published_at is not None
    assert first.published_at.year == 2026
    assert first.updated_at is not None


def test_parse_dedupes_by_id(loaded_repo):
    # 重复 upsert 同一批 → 第二次全为 updated=0 / new=0
    result = loaded_repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")))
    assert result["new"] == 0


def test_build_queries_contains_category_and_keywords():
    from paperpilot.config import TopicCfg

    topics = [TopicCfg(name="t", categories=["cs.CL"], keywords=["rag"])]
    queries = build_queries(topics, categories=["cs.AI"])
    assert any("cat:cs.CL" in q and 'all:"rag"' in q for q in queries)
    assert any("cat:cs.AI" in q for q in queries)
