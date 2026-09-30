"""画像池判据（2026-09-30 主题池化）：**主题 = 往画像池注入的词条包**。

这一批判据钉住的是从"主题规则表"换到"画像大池子"的过程中，**实测踩到的四个真坑**：

1. **种子冻结**：旧实现只在"画像完全为空"时播种一次 ⇒ 之后改主题画像永远不动
   （实测：库里 8 个作者只有 1 个与画像种子对得上，另外 4 个还是更早配置的中文名）。
2. **多词短语白占位**：`title_terms` 只抽单词，而主题关键词天然是短语 ⇒
   200 个画像键里 22 个永远命不中，而同一批词在日报线（子串匹配）活得好好的。
3. **撤权会误伤**：删主题如果直接删行，会把"行为学到的权重"一起丢掉 ⇒
   必须有 `source`/`w_base` 两列，只撤**基线**。
4. **负反馈没接线**：`skip_paper` 从前只改阅读态、完全不碰画像 ⇒
   `w<0` 一行都没有（"负向"那半个机制等于没接）。
"""

from __future__ import annotations

from sqlalchemy import select

from paperpilot.config import TopicCfg
from paperpilot.domain.policy import fallback_pool_score
from paperpilot.domain.profile import paper_features, pool_relevance, score_paper
from paperpilot.infra.arxiv import parse_atom
from paperpilot.infra.orm import ProfileWeight

from .conftest import SAMPLE_XML
from .test_ai_experience import _reg  # 同包夹具（容器 + 已入库样例）


def _paper(reg, idx: int = 0):
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    return papers[idx]


def _raw(repo) -> dict:
    """**原始累计值**（不做衰减读数——衰减按各自 now 算，天然带尾差，判幂等会假红）。"""
    with repo.sf() as s:
        return {(r.kind, r.key): (round(r.w, 9), round(r.w_base, 9), r.source, r.hits)
                for r in s.scalars(select(ProfileWeight)).all()}


# ---------------------------------------------------------------- 1) 结构：注入 / 幂等 / 撤权
def test_pool_injection_is_idempotent_and_revocable(tmp_path):
    """注入幂等（反复注不胖）、改权按差量、删主题只撤**基线**、学到的部分幸存。"""
    c, _reg_obj = _reg(tmp_path)
    repo = c.repo
    topics = [TopicCfg(name="T", keywords=["alpha", "beta"], weight=0.5)]
    repo.profile_reset(actor="human", reason="判据：清空重来")
    assert repo.profile_weights_map() == {}

    first = repo.sync_topic_pool(topics)
    assert first["injected"] == 2 and first["keys"] == 2
    snap = _raw(repo)
    assert snap[("term", "alpha")][2] == "topic:T"          # 溯源落上了
    assert snap[("term", "alpha")][1] == 0.5                # w_base = 注入基线

    # 幂等：再注一次，存储值一分不动
    again = repo.sync_topic_pool(topics)
    assert again["injected"] == 0 and _raw(repo) == snap

    # 改权重：按**差量**调（0.5→0.9 只加 0.4，不是再加 0.9）
    repo.sync_topic_pool([TopicCfg(name="T", keywords=["alpha", "beta"], weight=0.9)])
    assert _raw(repo)[("term", "alpha")][0] == 0.9

    # 给 alpha 攒一点"行为学分"，再删主题：基线撤掉、学分留下
    with repo.sf() as s:
        row = s.scalar(select(ProfileWeight).where(ProfileWeight.kind == "term",
                                                   ProfileWeight.key == "alpha"))
        row.w += 1.0        # 模拟信号加成
        row.hits += 3
        s.commit()
    res = repo.sync_topic_pool([])
    assert res["released"] == 2
    after = _raw(repo)
    assert ("term", "alpha") in after, "有学分的词被误删了"
    assert after[("term", "alpha")][0] == 1.0, "只该撤基线 0.5（1.5 − 0.5）"
    assert after[("term", "alpha")][1] == 0.0 and after[("term", "alpha")][2] == "signal"
    assert ("term", "beta") not in after, "无学分 + 基线归零 ⇒ 整行删掉（不留零权重噪声）"


def test_learned_weight_survives_topic_removal(tmp_path):
    """**判据核心**：删主题不能把"行为学到的权重"一起抹掉（这就是 source/w_base 的理由）。"""
    c, _ = _reg(tmp_path)
    repo = c.repo
    repo.profile_reset(actor="human", reason="判据：清空重来")
    repo.sync_topic_pool([TopicCfg(name="T", keywords=["rydberg"], weight=0.5)])

    with repo.sf() as s:
        row = s.scalar(select(ProfileWeight).where(ProfileWeight.kind == "term",
                                                   ProfileWeight.key == "rydberg"))
        row.w += 1.2          # 信号累计
        row.hits += 4         # 有命中 ⇒ 有学分
        s.commit()

    repo.sync_topic_pool([])                       # 删主题
    left = _raw(repo)
    assert ("term", "rydberg") in left, "有学分的行为权重被误删了"
    assert left[("term", "rydberg")][0] == 1.2, "只该撤掉基线 0.5，不该连学分一起撤"
    assert left[("term", "rydberg")][1] == 0.0 and left[("term", "rydberg")][2] == "signal"


# ---------------------------------------------------------------- 2) 短语不再是白占位
def test_multiword_keyword_actually_scores(tmp_path):
    """主题里的多词短语必须能**真的加分**（旧实现：永远命不中，白占位）。"""
    c, _ = _reg(tmp_path)
    repo = c.repo
    # 造一篇标题里正好含短语的论文特征
    feat = paper_features(["quant-ph"], "quant-ph",
                          "Neutral atom arrays for quantum simulation",
                          "We use neutral atom platforms…")
    phrase_w = {("term", "neutral atom"): 0.5}
    word_w = {("term", "neutral"): 0.5}
    s_phrase, why_phrase = score_paper(feat, phrase_w)
    s_word, _ = score_paper(feat, word_w)
    assert s_phrase > 0, "多词短语在画像里仍然命不中（白占位没修）"
    assert "短语命中" in " ".join(why_phrase)          # why 要说清是短语命中
    assert s_phrase > s_word, "短语更具体（系数 0.8 > 0.5），该比单词给得多"

    # 反例：短语不在文本里 ⇒ 不加分（不能变成"随便命中"）
    other = paper_features(["cs.CL"], "cs.CL", "Traffic forecasting with transformers")
    assert score_paper(other, phrase_w)[0] == 0.0
    assert repo is not None


def test_exclude_keywords_become_negative_weights(tmp_path):
    """排除词＝**负权重词条**（软惩罚），不再是硬门也照样能压下去。"""
    c, _ = _reg(tmp_path)
    repo = c.repo
    repo.profile_reset(actor="human", reason="判据：清空重来")
    repo.sync_topic_pool([TopicCfg(name="T", keywords=["rydberg"],
                                   exclude_keywords=["survey"], weight=0.5)])
    raw = _raw(repo)
    assert raw[("term", "survey")][0] < 0, "排除词没变成负权重"
    assert raw[("term", "survey")][1] < 0, "负权重也要记进 w_base，否则删主题撤不干净"

    feat = paper_features(["quant-ph"], "quant-ph", "A survey of Rydberg physics")
    raw_score, why = score_paper(feat, repo.profile_weights_map())
    assert any("survey" in w for w in why)
    assert raw_score < score_paper(feat, {("term", "rydberg"): 0.5})[0], "排除词该压低总分"


# ---------------------------------------------------------------- 3) 种子不再冻结
def test_topic_edits_reach_the_pool_every_time(tmp_path):
    """**冻结快照的回归判据**：改主题（加作者/加词）必须**立刻**反映到池子。

    旧实现只在空画像时播一次 ⇒ 这条会红（实测正是它让库里 8 个作者只对上 1 个）。
    """
    c, _ = _reg(tmp_path)
    repo = c.repo
    repo.profile_reset(actor="human", reason="判据：清空重来")
    repo.sync_topic_pool([TopicCfg(name="T", keywords=["alpha"], authors=["Alice"], weight=0.5)])
    assert ("author", "Alice") in _raw(repo)

    # 改：换作者 + 加词（画像**非空**——旧的"仅空画像才播种"在这里就再也不动了）
    repo.sync_topic_pool([TopicCfg(name="T", keywords=["alpha", "gamma"],
                                   authors=["Bob"], weight=0.5)])
    raw = _raw(repo)
    assert ("author", "Bob") in raw, "改过的作者没进画像（种子又被冻住了）"
    assert ("term", "gamma") in raw, "新加的词没进画像"
    assert ("author", "Alice") not in raw, "被移除的作者该撤权"


# ---------------------------------------------------------------- 5) 常驻页面
def test_profile_page_renders_the_pool(tmp_path):
    """`/profile` 常驻体检页：三件事必须画出来——权重构成、**哪个键在空转**、短语命中数。

    只做冒烟（用户口径"快做，不用做太多测试"）：渲染得出 + 关键要素在 + 数字与 repo 对得上。
    """
    from fastapi.testclient import TestClient

    from paperpilot.app.web import create_app

    c, _ = _reg(tmp_path)
    page = TestClient(create_app(c, None), follow_redirects=True).get("/profile")
    assert page.status_code == 200
    for probe in ("画像池体检", "信号学出来的", "暂未出现", "主题包", "多词短语已活化"):
        assert probe in page.text, f"页面缺少「{probe}」"
    assert 'href="/profile"' in page.text, "导航里没有入口"
    assert "{{" not in page.text and "{%" not in page.text, "模板有未渲染的残留"
    assert f"<b>{len(_raw(c.repo))}</b>" in page.text, "池中键总数与 repo 不一致"


# ---------------------------------------------------------------- 4) 负反馈接线
def test_legacy_seed_rows_get_a_baseline_and_can_be_released(tmp_path):
    """**老库回填判据**：`hits=0 且 w≠0` 的行只可能来自旧播种 ⇒ 把 `w` 认成基线，才撤得掉。

    实测事故：`w_base` 是新列，老行拿默认 0 ⇒ 撤权"减去 0"等于没撤，还被贴成 `signal`
    变成**幽灵权重**（4 个更早配置里的中文作者名以 0.5 权重阴魂不散，可视化里一眼看见）。
    """
    from paperpilot.infra.db import init_db
    from paperpilot.infra.orm import ProfileWeight

    from .test_ai_experience import _reg

    c, _ = _reg(tmp_path)
    repo = c.repo
    repo.profile_reset(actor="human", reason="判据：清空重来")
    engine = repo.sf.kw["bind"]

    # 造一行"旧播种"的样子：w=0.5、hits=0、w_base=0（新列默认值）、source 已被贴成 signal
    with repo.sf() as s:
        s.add(ProfileWeight(kind="author", key="旧种子作者", w=0.5, hits=0,
                            source="signal", w_base=0.0))
        s.commit()

    init_db(engine)                       # 再跑一次启动路径 ⇒ 回填 w_base
    with repo.sf() as s:
        row = s.scalar(select(ProfileWeight).where(ProfileWeight.key == "旧种子作者"))
        assert row is not None and row.w_base == 0.5, "回填没把旧播种行的 w 认成基线"

    repo.sync_topic_pool([])              # 没有主题要它 ⇒ 按孤儿基线撤掉
    assert ("author", "旧种子作者") not in _raw(repo), "幽灵基线没被撤掉"


def test_skip_and_star_feed_the_profile(tmp_path):
    """`skip_paper`/`star_paper`/`mark_read` 必须喂画像——**同一份意图，不管从哪条通道表达**。

    旧实现：Web 的「不感兴趣」记 `uninterested`(−1.5)，而 AI 的 `skip_paper` **只改阅读态**
    ⇒ 200 个权重全为正、`w<0` 一行都没有。
    """
    c, reg = _reg(tmp_path)
    repo = c.repo
    p = _paper(c)
    aid = p.arxiv_id

    before = repo.profile_weights_map()
    assert reg.invoke("skip_paper", arxiv_id=aid, actor="ai", reason="判据")["ok"]
    after = repo.profile_weights_map()
    # ⚠ 用**幅度**判（≤ −1.0）：两次读数之间有衰减尾差（~1e-9），只看"谁小了"会被噪声蒙混过关。
    # `uninterested` = −1.5 ⇒ 主分类键一次就吃到 −1.5；负向不设限幅（只有正向限幅）。
    deltas = {k: after[k] - before.get(k, 0.0) for k in after}
    assert min(deltas.values()) <= -1.0, (
        f"skip_paper 没往画像里写足够的负权重（负反馈仍断线）：最小增量 {min(deltas.values()):.4f}")

    before2 = repo.profile_weights_map()
    out = reg.invoke("star_paper", arxiv_id=aid, actor="ai", reason="判据")
    assert out["ok"]
    if out.get("star"):                     # 翻成"已收藏"才记 star 信号
        after2 = repo.profile_weights_map()
        gain = max(after2[k] - before2.get(k, 0.0) for k in after2)
        assert gain >= 0.1, f"star 没喂画像（最大正向增量 {gain:.4f}）"


def test_pool_relevance_is_a_documented_monotone_mapping(tmp_path):
    """兜底分映射是**明码**且单调：raw≤0→0、raw=1→0.5、raw=3→0.75（不是黑箱）。"""
    assert pool_relevance(-5) == 0.0 and pool_relevance(0) == 0.0
    assert pool_relevance(1) == 0.5 and pool_relevance(3) == 0.75
    vals = [pool_relevance(x) for x in (0.2, 0.5, 1, 2, 5)]
    assert vals == sorted(vals), "映射必须单调，否则排序会翻"
    assert all(0.0 <= v <= 1.0 for v in vals), "RelevanceScore 有 0..1 约束"

    # 兜底打分器与推荐流用**同一个** score_paper ⇒ 两条线一个真相
    c, _ = _reg(tmp_path)
    p = _paper(c)
    sc = fallback_pool_score(p, {("category", p.primary_category): 0.5})
    assert 0.0 <= sc.score <= 1.0 and sc.label in ("must_read", "worth", "skip")
    assert sc.reason, "拿不出 why 的条目不许进简报"
