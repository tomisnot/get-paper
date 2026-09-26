"""流水线端到端测试（离线，样例 Atom + Heuristic AI）。"""

from __future__ import annotations

from datetime import date


def test_pipeline_end_to_end(pipeline, loaded_repo):
    result = pipeline.run(force=True)

    assert result.error is None
    assert result.after_rules > 0
    assert 0 < result.selected <= pipeline.settings.scoring.max_papers
    assert result.degraded == []  # heuristic 全程可用

    briefing = loaded_repo.briefing_for_date(date.today().isoformat())
    assert briefing is not None
    assert "arXiv 每日简报" in briefing.markdown
    assert "今日必读" in briefing.markdown or "值得一看" in briefing.markdown
    assert briefing.stats["stats"]["ai_provider"] == "heuristic"
    assert len(briefing.stats["items"]) == result.selected

    # 状态机：入选进 in_briefing，其余过规则者进 archived，cs.ET 那篇不在任何主题内仍为 new
    counts = loaded_repo.counts_by_status()
    assert counts.get("in_briefing", 0) == result.selected
    assert counts.get("new", 0) == 1  # 2608.02555（cs.ET）不匹配任何主题分类
    assert sum(counts.values()) == 10


def test_pipeline_is_idempotent(pipeline, loaded_repo):
    first = pipeline.run(force=True)
    second = pipeline.run()  # 不 force → 复用
    assert second.reused is True
    assert second.briefing_id == first.briefing_id
    # 重跑后不产生第二份当天简报
    briefings = [b for b in loaded_repo.briefings() if b.date == first.date]
    assert len(briefings) == 1


def test_scores_and_summaries_persisted(pipeline, loaded_repo):
    pipeline.run(force=True)
    paper = loaded_repo.get_paper("2608.01101")
    assert paper is not None
    assert paper.scores, "打分应入库"
    assert all(0.0 <= s.score <= 1.0 for s in paper.scores)
    if paper.status == "in_briefing":
        assert paper.summaries and paper.summaries[0].tldr


def test_fts_search_finds_paper(pipeline, loaded_repo):
    pipeline.run(force=True)
    hits = loaded_repo.search_papers("speculative")
    assert any(p.arxiv_id == "2608.02444" for p in hits)
    assert loaded_repo.search_papers("zzzznonexistent") == []


def test_reading_state_and_notes(pipeline, loaded_repo):
    pipeline.run(force=True)
    paper = loaded_repo.get_paper("2608.01101")
    loaded_repo.set_read(paper, read=True)
    loaded_repo.add_note(paper, "值得精读")
    fresh = loaded_repo.get_paper("2608.01101")
    assert fresh.reading.read is True
    assert any(n.content == "值得精读" for n in fresh.notes)
    assert loaded_repo.toggle_star(fresh) is True
