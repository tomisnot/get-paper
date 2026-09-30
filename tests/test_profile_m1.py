"""M1 画像内核判据：taxonomy 纯函数、信号→权重方向、限幅、衰减、主题池注入、重置回滚。

纪律照旧：能红 + 不误报对偶；全部确定性（时钟靠 now 参数注入，不靠 sleep）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from check.test_ai_experience import _reg
from paperpilot.domain.profile import paper_features, score_paper, title_terms
from paperpilot.infra import arxiv_taxonomy as tax


# ---------------------------------------------------------------- taxonomy / 特征（纯函数）
def test_taxonomy_pure_helpers():
    assert tax.group_of("cs.CL") == "cs"
    assert tax.group_of("quant-ph") == "quant-ph"          # 无点自身即组
    assert tax.group_of("physics.optics") == "physics"
    assert "cs.AI" in tax.siblings_of("cs.CL")
    assert tax.siblings_of("nope.XX") == []                 # 未知分类安全回退
    assert "quant-ph" in tax.bridge_groups("cs.AI")
    assert tax.bridge_groups("nope.XX") == ()
    assert tax.is_known("cs.LG") and not tax.is_known("cs.LZ")
    assert len(set(tax.KNOWN)) == len(tax.KNOWN)           # 去重不变式（重复即红）


def test_title_terms_deterministic_and_stopword_free():
    t1 = title_terms("Rydberg Atom Arrays for Quantum Simulation and Sensing")
    assert "rydberg" in t1 and "quantum" in t1
    assert all(w not in ("for", "and", "the") for w in t1)
    assert t1 == title_terms("Rydberg Atom Arrays for Quantum Simulation and Sensing")
    assert len(t1) <= 8


def test_score_paper_linear_with_why():
    feat = paper_features(["quant-ph", "physics.atom-ph"], "quant-ph",
                          "Rydberg array", "", ["A. Author"])
    weights = {("category", "quant-ph"): 1.0, ("author", "A. Author"): 0.5}
    score, why = score_paper(feat, weights)
    assert score > 0 and why                                   # why 必须产出
    hi, _ = score_paper(feat, weights)
    lo, _ = score_paper(feat, {})                              # 空画像 ⇒ 0 分
    assert hi > lo


# ---------------------------------------------------------------- 信号→画像（repo 直调）
def test_signal_direction_cap_and_negatives(tmp_path):
    """能红：单事件正向限幅（限幅在乘系数之后统一生效）、负信号必降。

    ⚠ 2026-09-30 改：容器启动会把主题包注入画像池（`sync_topic_pool`）⇒ 主题声明过的分类
    **本来就有基线权重**（0.5）。所以这里断言**增量**而不是绝对值——旧写法假设"干净画像"，
    会把"主题先验 0.5 + 限幅后的信号 0.3 = 0.8"误判为限幅失效。
    """
    _c, reg = _reg(tmp_path)
    repo = _c.repo
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    repo.upsert_papers(papers, actor="human", reason="seed")
    p0 = papers[0]

    base = repo.profile_weights_map()
    repo.record_signal(p0.arxiv_id, "download", actor="human")
    w = repo.profile_weights_map()
    prim = ("category", p0.primary_category)
    # 主类 1.0×1.0 与副类 0.6×1.0 都被限幅到 POS_CAP ⇒ 增量恒等于上限
    assert w[prim] - base.get(prim, 0.0) == pytest.approx(repo.POS_CAP, abs=1e-6)
    for k, v in w.items():                     # 限幅在乘系数之后统一生效
        if k[0] == "category" and k != prim:
            # 容差 1e-6：两次读数之间有微秒级时间差 ⇒ 读侧衰减各带一点尾差（实测 ~1e-8）
            assert v - base.get(k, 0.0) <= repo.POS_CAP + 1e-6

    repo.record_signal(p0.arxiv_id, "uninterested", actor="human")
    w2 = repo.profile_weights_map()
    assert w2[prim] < w[prim]                  # 负信号必降（不放大但确实拉低）


def test_decay_half_life_at_read_side(tmp_path):
    _c, reg = _reg(tmp_path)
    repo = _c.repo
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML
    p0 = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))[0]
    repo.upsert_papers([p0], actor="human", reason="seed")
    repo.record_signal(p0.arxiv_id, "download", actor="human")
    now = repo.profile_weights_map()
    key = ("category", p0.primary_category)
    later = repo.profile_weights_map(half_life_days=30.0,
                                     now=datetime.utcnow() + timedelta(days=30))
    assert abs(later[key] - now[key] / 2) < 1e-3              # 一个半衰期 ⇒ 精确减半


def test_topic_pool_is_idempotent_and_reset_undo_roundtrip(tmp_path):
    """能红：主题包**幂等**注入画像池（反复注不会越注越胖）、先验不算 hits、
    改主题立刻反映到池子、重置可 undo 还原。

    这是取代旧 `profile_seed_if_empty`（"仅空画像才播种一次"⇒ 主题被冻成快照、后来改的
    作者永远进不去画像）的新语义判据。
    """
    _c, reg = _reg(tmp_path)
    repo = _c.repo
    topics = _c.settings.topics

    # 先清空池子，才能观察到"注入"本身（容器启动时已经注过一轮 ⇒ 直接调会是 injected=0）
    repo.profile_reset(actor="human", reason="清空以验注入")
    assert repo.profile_weights_map() == {}

    first = repo.sync_topic_pool(topics)
    assert first["keys"] > 0 and first["injected"] == first["keys"]   # 从空池注入：全新建
    view = repo.profile_view()
    assert view["total_hits"] == 0                             # 主题先验不算 hits（先验非行为）
    cats = {k for k, _w, _h in view["top"]["category"]}
    yaml_cats = {c for t in topics for c in t.categories}
    assert cats & yaml_cats                                    # 注入维度可验证

    # **幂等**：同一份主题再注一次，**原始累计值一分不动**。
    # （用 w 而不是衰减读数：衰减在读侧按各自的 now 计算，两次读数天然差 ~1e-8 尾差，
    #   拿它判幂等会假红——幂等说的是"存储的值不变"。）
    from paperpilot.infra.orm import ProfileWeight as _PW

    def raw() -> dict:
        with repo.sf() as s:
            return {(r.kind, r.key): round(r.w, 9) for r in s.scalars(select(_PW)).all()}

    snap = raw()
    again = repo.sync_topic_pool(topics)
    assert again["injected"] == 0 and again["released"] == 0
    assert raw() == snap

    # 溯源列可用：这些键的来源必须是主题包，才谈得上"删主题精确撤权"
    with repo.sf() as s:
        from paperpilot.infra.orm import ProfileWeight
        srcs = {r.source.split(":")[0] for r in s.scalars(select(ProfileWeight)).all()}
    assert "topic" in srcs

    snap_before = repo.profile_weights_map()
    r = repo.profile_reset(kind="category", actor="human", reason="测试重置")
    assert r["ok"] and r["removed"] > 0
    after = repo.profile_weights_map()
    assert set(after) == {k for k in snap_before if k[0] != "category"}   # 只清一维，键集精确
    u = repo.undo(0, actor="human", reason="撤销重置")
    assert u["ok"] and u["op"] == "profile_reset"
    assert repo.profile_weights_map().keys() >= snap_before.keys()   # 完整还原


def test_capability_surface_directions(tmp_path):
    _c, reg = _reg(tmp_path)
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML
    p0 = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))[0]
    bad = reg.invoke("record_signal", arxiv_id=p0.arxiv_id, signal="teleport")
    assert bad["ok"] is False and bad["error"]["kind"] == "bad_params"
    assert "uninterested" in bad["error"]["hint"]              # 可教学：枚举随错给出
    miss = reg.invoke("record_signal", arxiv_id="9999.00001", signal="download")
    assert miss["ok"] is False and miss["error"]["kind"] == "not_found"
    ok = reg.invoke("record_signal", arxiv_id=p0.arxiv_id, signal="download",
                    actor="ai", reason="用户说他下了")
    assert ok["ok"] and ok["source"] == "declared"
    gp = reg.invoke("get_profile")
    assert gp["ok"] and gp["top"]["category"] and gp["category_entropy"] >= 0.0
