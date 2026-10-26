from __future__ import annotations


class FakeScholar:
    """替身：不联网，返回固定边。"""

    def __init__(self, refs=None, cites=None):
        self._refs = refs or []
        self._cites = cites or []
        self.closed = False

    def references(self, arxiv_id, *, limit=40):
        return list(self._refs)

    def citations(self, arxiv_id, *, limit=40):
        return list(self._cites)

    def close(self):
        self.closed = True


def test_replace_edges_is_idempotent(ctx, paper):
    repo = ctx.repo
    aid = paper("2610.00090")
    rows = [{"arxiv_id": "1901.00001", "title": "Upstream", "year": 2019, "citations": 10},
            {"arxiv_id": "1901.00002", "title": "Other", "year": 2020, "citations": 3},
            {"arxiv_id": aid, "title": "self", "year": 2026}]
    first = repo.replace_edges(aid, rows)
    assert first["added"] == 2
    second = repo.replace_edges(aid, rows[:1])
    assert second["removed"] == 2 and second["added"] == 1
    assert [row["arxiv_id"] for row in repo.edges_from(aid)] == ["1901.00001"]


def test_merge_incoming_does_not_delete_other_edges(ctx, paper):
    repo = ctx.repo
    apex = paper("2610.00091")
    fan = paper("2610.00092")
    repo.replace_edges(fan, [{"arxiv_id": "1901.00003", "title": "Fan reference"}])
    repo.merge_incoming(apex, [{"arxiv_id": fan, "title": "Fan", "citations": 1}])
    assert "1901.00003" in [row["arxiv_id"] for row in repo.edges_from(fan)]
    assert [row["arxiv_id"] for row in repo.edges_to(apex)] == [fan]


def test_sync_reports_scholar_failure_as_teachable_error(ctx, stack, paper, monkeypatch):
    from paperpilot.scholar import ScholarError

    class Broken(FakeScholar):
        def references(self, arxiv_id, *, limit=40):
            raise ScholarError("429 限速")

    aid = paper("2610.00101")
    monkeypatch.setattr(ctx, "scholar", lambda: Broken())
    result = stack.tools.execute("sync_citations", {"arxiv_id": aid})
    assert result["is_error"] is True
    assert result["error"]["info"]["kind"] == "scholar_unavailable"
    assert result["error"]["info"]["hint"]
