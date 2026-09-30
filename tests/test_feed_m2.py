"""M2 feed 判据：配比解析/熵保底（纯函数）→ 配额与交织（确定性）→ 端到端与 max_items。

对偶纪律：每样能力至少一条红证 + 一条不误报。
"""

from __future__ import annotations

from check.test_ai_experience import _reg
from paperpilot.domain.feed import (
    EXPLORE_FLOOR_PCT,
    allocate,
    maybe_entropy_boost,
    resolve_quotas,
)


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


def test_feed_page_reuses_digest_card(tmp_path):
    """能红（UI 复用）：/feed 卡片与简报同款——评过的篇目带 TL;DR 与
    上次评审理由；没评过的给指位文案（不假装有摘要）。"""
    _c, reg = _reg(tmp_path)
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from paperpilot.app.web import create_app
    from paperpilot.infra.arxiv import parse_atom
    from paperpilot.infra.orm import Paper, PaperScore, PaperSummaryRow

    from .conftest import SAMPLE_XML
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c.repo.upsert_papers(papers, actor="human", reason="seed")
    reg.invoke("record_signal", arxiv_id=papers[0].arxiv_id, signal="download")
    with _c.repo.sf() as s:
        pid = s.scalar(select(Paper.id).where(Paper.arxiv_id == papers[0].arxiv_id))
        s.add(PaperSummaryRow(run_id="r1", paper_id=pid, tldr="一句话结论",
                              problem="问题", method="方法", results="果",
                              novelty="献", model="dsh-test"))
        s.add(PaperScore(run_id="r1", paper_id=pid, score=0.9, label="must_read",
                         reason="与你的方向很相关", model="dsh-test"))
        s.commit()

    client = TestClient(create_app(_c, None))
    r = client.get("/feed?preview=1&days=120&limit=10&seen_days=0")   # 预览通道（不落库）
    assert r.status_code == 200
    body = r.text
    assert "🌊 推荐流" in body and "为什么推荐给你" in body
    assert "TL;DR" in body and "一句话结论" in body           # 简报同款摘要块直接复用
    assert "与你的方向很相关" in body                        # 评审过⇒理由优先于道属
    assert "还没有中文摘要" in body                          # 没评过的给指路，不装


def test_feed_refresh_cursor_is_ai_owned(tmp_path):
    """能红（刷新自由归 AI）：offset 换屏不重喂不空转，回执带 next_offset 自续标；
    越过池底不静默——notes 里给出路；每次刷先进归因总线（监控/记录仪可见性）。"""
    _c, reg = _reg(tmp_path)
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c.repo.upsert_papers(papers, actor="human", reason="seed")
    reg.invoke("record_signal", arxiv_id=papers[0].arxiv_id, signal="download")

    s1 = reg.invoke("feed_generate", limit=3, days=120, seen_days=0)
    assert s1["ok"] and s1["count"] == 3
    ids1 = [e["arxiv_id"] for e in s1["feed"]]
    s2 = reg.invoke("feed_generate", limit=3, days=120, seen_days=0,
                    offset=s1["meta"]["next_offset"])
    assert s2["ok"] and s2["count"] == 3
    ids2 = [e["arxiv_id"] for e in s2["feed"]]
    assert set(ids1).isdisjoint(ids2)                      # 换屏不重喂
    assert s2["meta"]["next_offset"] == 6                  # 游标自续，AI 自己拿得走

    drained = reg.invoke("feed_generate", limit=3, days=120, seen_days=0, offset=999)
    assert drained["ok"] and drained["count"] == 0
    assert any("池底" in n for n in drained["meta"]["notes"])   # 见底不静默，给出路

    ev = reg.invoke("get_activity", op="telemetry.feed_generate")   # ⚠ op 名带 telemetry. 前缀（本批改）
    assert ev["events_count"] >= 3                          # 每次刷都进记录仪（谁在刷可查）
    assert ev["events"][-1]["after"]["lanes"]                  # 留痕带得够诊断的料


def test_publish_feed_issue_model(tmp_path):
    """能红（期票模型，用户规格）：publish_feed 写一期，/feed 默认只读最新期不重算；
    换页=offset 续 next_offset 零重叠；preview 临时预览不落库；save_feed 进总线且可撤销回上期。"""
    _c, reg = _reg(tmp_path)
    from fastapi.testclient import TestClient

    from paperpilot.app.web import create_app
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c.repo.upsert_papers(papers, actor="human", reason="seed")
    reg.invoke("record_signal", arxiv_id=papers[0].arxiv_id, signal="download")

    p1 = reg.invoke("publish_feed", limit=3, days=120, seen_days=0, reason="第一期")
    assert p1["ok"] and p1["count"] == 3 and p1["issue_id"], p1
    p2 = reg.invoke("publish_feed", limit=3, days=120, seen_days=0,
                    offset=p1["meta"]["next_offset"], reason="第二页")
    assert p2["ok"] and p2["issue_id"] > p1["issue_id"]
    # publish 回执不重送 feed 体（快照已入库），从库里取两期对账：
    from sqlalchemy import select

    from paperpilot.infra.orm import FeedIssue
    with _c.repo.sf() as s:
        rows = s.scalars(select(FeedIssue).order_by(FeedIssue.id)).all()
    i1 = [x["arxiv_id"] for x in rows[0].items]
    i2 = [x["arxiv_id"] for x in rows[1].items]
    assert set(i1).isdisjoint(i2)                            # 换页零重叠

    client = TestClient(create_app(_c, None))
    body = client.get("/feed").text                          # 默认：只读最新期
    assert "第 2 期" in body and "第二页" in body            # 参数/缘由各归其期，面板不自行重算
    assert "limit=3" in body and "days=120" in body

    n_before = _c.repo.feed_issue_count()
    client.get("/feed?preview=1&limit=5&days=120&seen_days=0")
    assert _c.repo.feed_issue_count() == n_before            # 预览不落库不覆盖期

    ev = reg.invoke("get_activity", op="save_feed")
    assert ev["events_count"] == 2
    u = reg.invoke("undo", seq=0)
    assert u["ok"] and u["op"] == "save_feed"
    assert _c.repo.latest_feed_issue().id == rows[0].id      # 撤掉第二期，回到第一期


def test_write_summary_single_paper_card(tmp_path):
    """能红（日报同款单篇补卡）：write_summary 写的卡被 read_paper/feed 面板自动复用；
    半张卡/乱 label 响亮拒；undo 撤整张。"""
    _c, reg = _reg(tmp_path)
    from fastapi.testclient import TestClient

    from paperpilot.app.web import create_app
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c.repo.upsert_papers(papers, actor="human", reason="seed")
    p0 = papers[0]

    # 不误报：拒半张卡、拒野 label、拒空卡、拒不在库
    assert reg.invoke("write_summary", arxiv_id="9999.00001", tldr="x")["error"]["kind"] == "not_found"
    assert reg.invoke("write_summary", arxiv_id=p0.arxiv_id, score=0.8)["error"]["kind"] == "bad_params"
    assert reg.invoke("write_summary", arxiv_id=p0.arxiv_id,
                      score=0.8, label="maybe")["error"]["kind"] == "bad_params"
    assert reg.invoke("write_summary", arxiv_id=p0.arxiv_id)["error"]["kind"] == "no_fields"

    ok = reg.invoke("write_summary", arxiv_id=p0.arxiv_id, tldr="一句话总括",
                    novelty="贡献点", keywords="rydberg, thermalization",
                    score=0.86, label="must_read", reason="方向核心", actor="ai")
    assert ok["ok"] and ok["wrote_score"] and ok["run_id"].startswith("card-")
    d = reg.invoke("get_paper", arxiv_id=p0.arxiv_id)
    assert d["summary"]["tldr"] == "一句话总括"
    assert d["summary"]["keywords"] == ["rydberg", "thermalization"]
    assert any(sc["label"] == "must_read" and sc["score"] == 0.86 for sc in d["scores"])

    # feed 面板复用（预览通道即同一 join 路径）：卡面出现 TL;DR
    client = TestClient(create_app(_c, None))
    body = client.get("/feed?preview=1&days=120&limit=25&seen_days=0").text
    assert "一句话总括" in body

    # 撤整张：总结行+评分行同 run_id 成对删
    u = reg.invoke("undo", seq=0)
    assert u["ok"] and u["op"] == "write_summary"
    d2 = reg.invoke("get_paper", arxiv_id=p0.arxiv_id)
    assert d2["summary"] is None
    assert all(sc["score"] != 0.86 for sc in d2["scores"])


# ---------------------------------------------------------------- M4 调研基建
class _FakeScholar:
    def __init__(self, *a, **k):
        pass

    def references(self, ext_id, *, limit=100):
        return [
            {"citedPaper": {"externalIds": {"ArXiv": "1512.03385"}, "title": "ResNet",
                            "citationCount": 90000}, "isInfluential": True},
            {"citedPaper": {"externalIds": {"ArXiv": "1706.03762"}, "title": "Attention",
                            "citationCount": 80000}, "isInfluential": False},
            {"citedPaper": {"title": "no-arxiv-here"}, "isInfluential": False},   # 无号边被丢
        ]

    def close(self):
        pass


def test_citation_edges_upstream_related_undo(tmp_path, monkeypatch):
    """能红：sync 落边（无 arXiv 号的丢弃、重跑幂等）；upstream 认多篇同引；
    related 共引交集；undo 撤本轮边回快照。"""
    from sqlalchemy import select

    import paperpilot.capabilities.tools as tools_mod

    _c, reg = _reg(tmp_path)
    from paperpilot.infra.arxiv import parse_atom
    from paperpilot.infra.orm import CitationEdge

    from .conftest import SAMPLE_XML
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c.repo.upsert_papers(papers, actor="human", reason="seed")
    monkeypatch.setattr(tools_mod, "SemanticScholarClient", _FakeScholar)
    a, b = papers[0].arxiv_id, papers[1].arxiv_id

    r = reg.invoke("sync_citations", arxiv_id=a, reason="测网络")
    assert r["ok"] and r["edges"] == 2                      # 第三条无号丢弃
    r2 = reg.invoke("sync_citations", arxiv_id=a)           # 重跑幂等
    assert r2["ok"] and r2["edges"] == 2 and r2["replaced"] == 2
    reg.invoke("sync_citations", arxiv_id=b)
    with _c.repo.sf() as s:
        assert len(s.scalars(select(CitationEdge)).all()) == 4   # 2 src × 2 dst，没膨胀

    up = reg.invoke("upstream_clusters")
    assert up["ok"] and up["count"] == 2
    resnet = [g for g in up["clusters"] if g["dst_arxiv_id"] == "1512.03385"][0]
    assert resnet["count"] == 2 and set(resnet["cited_by"]) == {a, b}

    rel = reg.invoke("related_papers", arxiv_id=a)
    assert rel["ok"] and rel["related"][0]["arxiv_id"] == b
    assert rel["related"][0]["shared_references"] == 2

    u = reg.invoke("undo", seq=0)                            # 撤 b 的本轮：快照为空 ⇒ 删光 b 边
    assert u["ok"] and u["op"] == "sync_citations"
    with _c.repo.sf() as s:
        assert len(s.scalars(select(CitationEdge).where(
            CitationEdge.src_arxiv_id == b)).all()) == 0


def test_coverage_and_timeseries_and_watch(tmp_path, monkeypatch):
    """能红：覆盖率对账；趋势聚合含信号计数；作者监控=主题 authors ∪ 画像作者，
    无作者响亮 no_authors；无库时不误写。"""
    import paperpilot.capabilities.tools as tools_mod
    _c, reg = _reg(tmp_path)
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c.repo.upsert_papers(papers, actor="human", reason="seed")

    # 作者监控空态必须第一时间验（后面的 download 信号会把论文作者进画像正权）：
    no = reg.invoke("watch_authors")
    assert no["ok"] is False and no["error"]["kind"] == "no_authors"

    cov0 = reg.invoke("coverage_report")
    assert cov0["ok"] and cov0["total"] == len(papers) and cov0["with_summary"] == 0
    reg.invoke("write_summary", arxiv_id=papers[0].arxiv_id, tldr="卡一", reason="测覆盖")
    cov1 = reg.invoke("coverage_report")
    assert cov1["with_summary"] == 1 and cov1["coverage_pct"] > 0
    assert all(m["arxiv_id"] != papers[0].arxiv_id for m in cov1["missing_sample"])  # 有卡的不进缺卡单

    reg.invoke("record_signal", arxiv_id=papers[1].arxiv_id, signal="download")
    st = reg.invoke("stats_timeseries", days=30)
    assert st["ok"] and st["signals"]["signal:download"] >= 1
    assert sum(st["daily_new"].values()) >= len(papers)      # 入库日聚合对得上
    assert reg.invoke("stats_timeseries", days=0)["ok"] is False

    reg.invoke("update_topic", name=_c.settings.topics[0].name, authors="Lukin, 陈妄")

    class _FakeArxiv:
        def __init__(self, *a, **k):
            pass

        def fetch_authors(self, names, *, since=None, max_per_author=25):
            return {n: papers[:1] for n in names}

        def close(self):
            pass

    monkeypatch.setattr(tools_mod, "ArxivClient", _FakeArxiv)
    w = reg.invoke("watch_authors", days=7)
    assert w["ok"] and {"Lukin", "陈妄"} <= set(w["watched"])   # 主题作者必在监控里
    # 画像正权作者（前面 download 带进去的论文作者）也在——两路汇入就是设计行为
    assert set(w["authors"]) == set(w["watched"])                # 每位受监控者都有报告条目
    assert w["authors"]["Lukin"][0]["arxiv_id"] == papers[0].arxiv_id
