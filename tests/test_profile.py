from __future__ import annotations

from paperpilot import db, scoring


def test_recompute_turns_signals_into_weights(ctx):
    repo = ctx.repo
    paper = repo.upsert_papers([{
        "arxiv_id": "2610.00010", "title": "Neutral atom transport in optical tweezers",
        "authors": ["Alice"], "categories": ["quant-ph", "physics.atom-ph"],
        "primary_category": "quant-ph"}], found_by="test")
    assert paper["new"] == 1
    repo.write_card("2610.00010", {"tldr": "t", "terms": ["neutral atom", "optical tweezer"]})
    repo.record(actor="human", op="signal:star", target="2610.00010", after={})
    repo.record(actor="human", op="signal:view", target="2610.00010", after={})

    stats = scoring.recompute_profile(repo, ctx.settings.signal_weights)
    assert stats["signals"] == 2
    snapshot = scoring.profile_snapshot(repo, ctx.settings.half_life_days)
    assert snapshot["category"]["quant-ph"]["w"] > 0
    assert "neutral atom" in snapshot["term"]
    assert snapshot["author"]["Alice"]["hits"] == 2


def test_decay_is_read_side(ctx):
    """把时间推到半个半衰期之前，读数应该正好是一半；原始值不动。"""
    repo = ctx.repo
    scoring.set_anchor(repo, dimension="category", key="quant-ph", weight=1.0)
    half_life = ctx.settings.half_life_days
    repo.conn.execute("UPDATE profile_weights SET updated_at=? WHERE key='quant-ph'",
                      (db.days_ago_iso(half_life),))
    repo.conn.commit()
    snapshot = scoring.profile_snapshot(repo, half_life)
    assert 0.49 < snapshot["category"]["quant-ph"]["w"] < 0.51
    assert snapshot["category"]["quant-ph"]["w_raw"] == 1.0


def test_anchor_survives_recompute(ctx):
    repo = ctx.repo
    scoring.set_anchor(repo, dimension="term", key="rydberg", weight=2.0)
    scoring.recompute_profile(repo, ctx.settings.signal_weights)
    snapshot = scoring.profile_snapshot(repo, ctx.settings.half_life_days)
    assert snapshot["term"]["rydberg"]["source"] == "anchor"
    assert snapshot["term"]["rydberg"]["w"] > 1.9


def test_phrase_hit_scores_paper_without_card(ctx, paper):
    """没写卡的论文也要能被"画像里的短语"命中——这是词面维从 0% 到可用的关键。"""
    repo = ctx.repo
    paper("2610.06001", title="Neutral atom transport in optical tweezer arrays")
    scoring.set_anchor(repo, dimension="term", key="optical tweezer array", weight=5.0)
    features = scoring.paper_features(repo.paper("2610.06001"), None)   # 没有卡
    snapshot = scoring.profile_snapshot(repo, ctx.settings.half_life_days)
    value, why = scoring.score(features, snapshot)
    assert value > 0
    assert any("短语" in reason for reason in why)


def test_single_word_keys_do_not_phrase_match(ctx, paper):
    """卡里学出来的 "quantum" 这类单字泛词不参与命中，否则等于给全库加常数。"""
    repo = ctx.repo
    paper("2610.06020", title="A quantum device", authors=["Nobody"])
    scoring.set_anchor(repo, dimension="term", key="quantum", weight=9.0)
    snapshot = scoring.profile_snapshot(repo, ctx.settings.half_life_days)
    _, why = scoring.score(scoring.paper_features(repo.paper("2610.06020"), None),
                           snapshot)
    assert not any("短语" in reason for reason in why)


def test_category_is_normalized_by_max_not_sum(ctx, paper):
    """命中头号分类就该拿满该维的 α，不再被"维里键太多"摊薄。"""
    repo = ctx.repo
    paper("2610.06030", categories=["quant-ph"])
    scoring.set_anchor(repo, dimension="category", key="quant-ph", weight=10.0)
    for index in range(5):
        scoring.set_anchor(repo, dimension="category", key=f"other-{index}", weight=1.0)
    snapshot = scoring.profile_snapshot(repo, ctx.settings.half_life_days)
    value, why = scoring.score(scoring.paper_features(repo.paper("2610.06030"), None),
                               snapshot)
    assert abs(value - scoring.DIMENSION_ALPHA["category"]) < 1e-6
    assert any("分类 quant-ph" in reason for reason in why)
