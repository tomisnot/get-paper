"""M2 feed 判据：配比解析/熵保底（纯函数）→ 配额与交织（确定性）→ 端到端与 max_items。

对偶纪律：每样能力至少一条红证 + 一条不误报。
"""

from __future__ import annotations

from paperpilot.domain.feed import (
    EXPLORE_FLOOR_PCT,
    allocate,
    maybe_entropy_boost,
    resolve_quotas,
)

from .test_ai_experience import _reg


def _item(i: str, cat: str, score: float, authors=("A",)) -> dict:
    return {"arxiv_id": i, "title": f"t{i}", "primary_category": cat,
            "categories": [cat], "authors": list(authors), "published": "",
            "score": score, "why": [f"w{i}"], "lane": "?"}


def _test_items(specs) -> list[dict]:
    """按 (id, cat, score) 造条目，**每人给独名作者**（真实世界如此；全同名会被
    per-author≤1 正当拦掉——那是重排规则在干活，不是 bug）。"""
    return [_item(i, c, s, authors=(f"Auth-{i}",)) for i, c, s in specs]


def _buckets(**kw) -> dict:
    b = {"primary": [], "adjacent": [], "hot": [], "explore": []}
    for lane, items in kw.items():
        b[lane] = items
    for lane in b:
        for it in b[lane]:
            it["lane"] = lane
    return b


# ---------------------------------------------------------------- 配比（纯函数）
def test_resolve_quotas_floor_cannot_be_breached():
    q, note = resolve_quotas("auto", "80,10,5,5")     # 探索 5% 想压穿地板
    assert q[3] * 100 >= sum(q) * EXPLORE_FLOOR_PCT   # 顶回来了
    assert q[0] < 80 and "地板" in note               # 从主兴趣道挪的，且吱声
    q2, n2 = resolve_quotas("explorer", "")
    assert q2 == (30, 20, 10, 40) and n2 == ""        # 不误报：合法预设原样通过
    q3, n3 = resolve_quotas("auto", "1,2")            # 坏输入响亮降级不抛
    assert q3 == (45, 25, 10, 20) and "4 个" in n3


def test_entropy_boost_only_when_narrow():
    q, note = maybe_entropy_boost((45, 25, 10, 20), entropy=0.5)
    assert q[3] > 20 and q[0] < 45 and "加倍" in note  # 画像收窄 ⇒ 探索加倍
    q2, note2 = maybe_entropy_boost((45, 25, 10, 20), entropy=3.2)
    assert q2 == (45, 25, 10, 20) and note2 == ""      # 不误报：宽画像不动手


# ---------------------------------------------------------------- 装配（确定性）
def test_allocate_caps_interleave_and_determinism():
    prim = _test_items([(f"p{i}", "cs.CL" if i < 7 else "cs.LG", float(10 - i))
                        for i in range(1, 11)])
    exp = _test_items([(f"e{i}", "q-bio.PE", 1.0) for i in range(1, 6)])
    buckets = _buckets(primary=prim, explore=exp)
    r1 = allocate(buckets, limit=10, quotas=(45, 25, 10, 20))
    r2 = allocate(_buckets(primary=[dict(x) for x in prim],
                           explore=[dict(x) for x in exp]),
                  limit=10, quotas=(45, 25, 10, 20))
    assert [x["arxiv_id"] for x in r1] == [x["arxiv_id"] for x in r2]   # 可复算
    assert sum(1 for x in r1 if x["primary_category"] == "cs.CL") <= 3  # 分类打散
    assert sum(1 for x in r1 if x["lane"] == "explore") >= 1            # 地板生效
    idx = [i for i, x in enumerate(r1) if x["lane"] == "explore"]
    assert idx and min(idx) <= 5                                       # 探索不沉底
    assert all(x["why"] for x in r1)                                   # 无 why 不展示


# ---------------------------------------------------------------- 端到端（能力面）
def test_feed_generate_end_to_end_and_seen(tmp_path):
    _c, reg = _reg(tmp_path)
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c.repo.upsert_papers(papers, actor="human", reason="seed")
    p0 = papers[0]
    reg.invoke("record_signal", arxiv_id=p0.arxiv_id, signal="download")

    out = reg.invoke("feed_generate", limit=10, days=120, seen_days=7)
    assert out["ok"], out
    assert out["count"] >= 1 and out["meta"]["category_entropy"] >= 0
    assert all(e["why"] for e in out["feed"])              # 探索条目也要有可读理由
    first = out["feed"][0]["arxiv_id"]

    # seen 去重：记 view 后，同一篇不再端出（换屏不重复喂）
    _c.repo.record_signal(first, "view", actor="human")
    again = reg.invoke("feed_generate", limit=10, days=120, seen_days=7)
    assert first not in [e["arxiv_id"] for e in again["feed"]]
    # 不误报：坏 limit 响亮
    bad = reg.invoke("feed_generate", limit=0)
    assert bad["ok"] is False and bad["error"]["kind"] == "bad_params"


def test_finalize_max_items_explicit_beats_config(tmp_path):
    """能红：finalize(max_items=2) 真封顶 2（yaml 默认 12 不动）；不传=旧行为。"""
    _c, reg = _reg(tmp_path)
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML
    _c.repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")),
                           actor="human", reason="seed")
    prep = reg.invoke("prepare_review")
    assert prep["ok"], prep
    before_max = _c.settings.scoring.max_papers             # conftest 实值（别假设 12）
    reviews = [{"arxiv_id": c["arxiv_id"], "score": 0.95, "label": "worth",
                "reason": "全票"} for c in prep["candidates"]]
    assert reg.invoke("submit_review", reviews=reviews)["ok"]
    fin = reg.invoke("finalize_briefing", force=True, max_items=2)
    assert fin["ok"] and fin["selected"] <= 2, fin
    assert fin["max_items_applied"] == 2
    assert _c.settings.scoring.max_papers == before_max          # 共享态一字未动
    plain = reg.invoke("finalize_briefing", force=True)          # 不传=按配置（>2）
    assert plain["ok"] and plain["selected"] > 2, plain
