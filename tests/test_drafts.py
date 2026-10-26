from __future__ import annotations

import pytest

from paperpilot import drafts
from paperpilot.drafts import DraftError


def test_linear_rejects_unknown_paper(ctx):
    with pytest.raises(DraftError) as excinfo:
        drafts.normalize("linear", {"entries": [{"arxiv_id": "2610.99999", "why": "x"}]},
                         ctx.repo)
    assert "不在库里" in str(excinfo.value)


def test_network_rejects_edge_outside_nodes(ctx, paper):
    aid = paper("2610.00034")
    with pytest.raises(DraftError) as excinfo:
        drafts.normalize("network", {
            "nodes": [{"arxiv_id": aid}],
            "edges": [{"src": aid, "dst": "2610.99998"}]}, ctx.repo)
    assert "端点" in str(excinfo.value)


def test_checkup_flags_missing_cards_and_repeats(ctx, paper):
    first, second = paper("2610.00035"), paper("2610.00036")
    ctx.repo.write_card(first, {"tldr": "有卡"})
    clean = drafts.normalize("linear", {"entries": [
        {"arxiv_id": first, "why": "a"}, {"arxiv_id": second, "why": "b"}]}, ctx.repo)
    checks = drafts.checkup(ctx.repo, "linear", clean, recent_days=7)
    assert checks["no_card"] == [second]
    assert checks["recently_shown"] == []

    ctx.repo.save_draft(kind="linear", title="上一份", note="", payload=clean)
    again = drafts.checkup(ctx.repo, "linear", clean, recent_days=7)
    assert sorted(again["recently_shown"]) == sorted([first, second])
    assert again["top_share"] == 1.0
