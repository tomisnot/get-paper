from __future__ import annotations

from paperpilot.ids import base_id, split_id


def test_upsert_is_idempotent_and_keeps_first_found_by(ctx):
    repo = ctx.repo
    first = repo.upsert_papers([{"arxiv_id": "2610.00001", "title": "A", "categories": ["quant-ph"],
                                 "primary_category": "quant-ph"}], found_by="region:quant-ph@2026-03-01")
    assert first["new"] == 1
    second = repo.upsert_papers([{"arxiv_id": "2610.00001", "title": "A revised",
                                  "categories": ["quant-ph"], "primary_category": "quant-ph"}],
                                found_by="query:other@2026-04-01")
    assert second["updated"] == 1 and second["new"] == 0
    paper = repo.paper("2610.00001")
    assert paper["title"] == "A revised"
    assert paper["found_by"] == "region:quant-ph@2026-03-01"


def test_card_and_reading_roundtrip(ctx, paper):
    repo = ctx.repo
    aid = paper("2610.00002")
    assert repo.write_card(aid, {"tldr": "一句话", "terms": ["neutral atom"]})
    card = repo.card(repo.paper(aid)["id"])
    assert card["tldr"] == "一句话"
    assert card["terms"] == ["neutral atom"]

    repo.set_state(aid, "starred", True)
    assert repo.paper_view(aid)["reading"]["starred"] is True
    assert repo.star_map([aid]) == {aid: True}
