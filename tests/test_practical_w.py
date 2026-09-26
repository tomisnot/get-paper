"""实用工单（PRACTICAL-UPGRADE 2026-09-26）判据 —— 每条"能红 + 不误报"对偶。

覆盖：W4 评审 floor（含"滤空 ⇒ 诊断点名 review_floor"）、W8 fetch_paper_by_id
（幂等/未知 id 响亮/mecha 投影）、W9 批量阅读态（per-item、单 id 旧形状不破）、
"删除定时任务"守卫（模块/配置字段不复存在）、W1 空池 reason 带计数（R2 升级项）。
"""

from __future__ import annotations

import importlib.util

import pytest

from .test_ai_experience import _drain_pool, _reg


# ---------------------------------------------------------------- W4 评审 floor
def test_w4_floor_filters_payload_and_reports(tmp_path):
    """能红：floor 高于全部基线 ⇒ 候选被滤空，诊断点名 review_floor 并给出路。"""
    _c, reg = _reg(tmp_path)
    container = _c
    container.settings.scoring.review_floor = 0.99      # 几乎滤掉一切
    out = reg.invoke("prepare_review")
    assert out["ok"] is False, f"应滤空并诊断：{out}"
    err = out["error"]
    assert err["kind"] == "empty_pool"
    assert "review_floor" in err["message"] or "review_floor" in err["hint"]
    assert "scoring.review_floor" in err["hint"]        # 出路点名可调的旋钮


def test_w4_floor_keeps_high_baselines(tmp_path):
    """能红（中间态）：以全量候选的最低基线为 floor ⇒ 一篇不滤、floor_applied 自证。"""
    _c, reg = _reg(tmp_path)
    _c.settings.scoring.review_floor = 0.0
    full = reg.invoke("prepare_review")
    assert full["ok"] and full["candidates"]
    scores = [c["baseline"]["score"] for c in full["candidates"]]
    _c.settings.scoring.review_floor = min(scores)      # 恰不滤掉任何一篇
    kept = reg.invoke("prepare_review")
    assert kept["ok"]
    fa = kept["floor_applied"]
    assert fa["kept"] == fa["total"] == len(full["candidates"])


def test_w4_zero_floor_no_false_filter(tmp_path):
    """不误报：floor=0 ⇒ 全量进 payload、ok=True（不拿"滤空"误伤正常场景）。"""
    _c, reg = _reg(tmp_path)
    _c.settings.scoring.review_floor = 0.0
    out = reg.invoke("prepare_review")
    assert out["ok"] is True
    assert out["floor_applied"]["kept"] == out["floor_applied"]["total"]
    assert out["candidates"]


# ---------------------------------------------------------------- W8 按 id 入库
class _FakeArxiv:
    """替身：仅当请求 id 命中 known 才返回论文（不联网）；calls 记录请求。"""

    def __init__(self, known_ids, paper):
        self._known = set(known_ids)
        self._paper = paper
        self.calls: list = []

    def __call__(self, **_kwargs):        # 冒充 ArxivClient(cache_dir=...) 构造
        return self

    def fetch_by_ids(self, ids):
        self.calls.append(list(ids))
        want = str(ids[0]).strip() if ids else ""
        return [self._paper] if want in self._known else []

    def close(self):
        pass


def test_w8_fetch_by_id_registers_idempotent_and_teaches(tmp_path, monkeypatch):
    """能红：库外单篇入库→幂等 cached→未知 id 响亮 not_found；全程不联网（替身）。"""
    from dataclasses import replace

    from paperpilot.capabilities import tools as cap_tools
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML

    # 造一篇库里没有的（_reg 已装入全部样例，换 id 才测得到"新入库"）
    paper = replace(parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))[0],
                    arxiv_id="2401.00001")
    fake = _FakeArxiv([paper.arxiv_id], paper)
    monkeypatch.setattr(cap_tools, "ArxivClient", fake)
    _c, reg = _reg(tmp_path)

    first = reg.invoke("fetch_paper_by_id", arxiv_id=paper.arxiv_id, reason="测试拉入")
    assert first["ok"] and first["new"] == 1 and not first["cached"], first
    seen = reg.invoke("get_paper", arxiv_id=paper.arxiv_id)
    assert seen["ok"] and seen["paper"]["arxiv_id"] == paper.arxiv_id

    again = reg.invoke("fetch_paper_by_id", arxiv_id=paper.arxiv_id)   # 同替身重放
    assert again["ok"] and again["new"] == 0 and again["cached"] is True

    miss = reg.invoke("fetch_paper_by_id", arxiv_id="9999.99999")
    assert miss["ok"] is False and miss["error"]["kind"] == "not_found"
    assert "1706" in miss["error"]["hint"]              # 可教学：给出 id 形状
    assert reg.invoke("get_paper", arxiv_id="9999.99999")["ok"] is False  # 未误入库


def test_w8_tool_projected_on_mcp_surface(tmp_path):
    """投影守卫：fetch_paper_by_id 上模型可见面，且必填 arxiv_id 不塌。"""
    from .test_mecha_adapter import _stack

    _c2, stack = _stack(tmp_path)
    names = {s["name"] for s in stack["tools"].schemas()}
    assert "fetch_paper_by_id" in names
    req = stack["required_source"].required_for("fetch_paper_by_id", None)
    assert req == ["arxiv_id"]


# ---------------------------------------------------------------- W9 批量阅读态
def test_w9_bulk_mark_read_per_item(tmp_path):
    """能红：两好一坏一次调用——2 篇落事件、坏 id 进 rejected 不伤其余。"""
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML

    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c, reg = _reg(tmp_path)
    ids = [papers[0].arxiv_id, papers[1].arxiv_id]
    out = reg.invoke("mark_read", arxiv_id=",".join(ids) + ",0000.00001",
                     read=True, reason="批量测试")
    assert out["ok"] and sorted(out["arxiv_ids"]) == sorted(ids)
    assert out["rejected"] == [{"arxiv_id": "0000.00001", "why": "库里没有这篇"}]


def test_w9_single_id_keeps_legacy_shape(tmp_path):
    """不误报：单 id 返回旧形状（arxiv_id/read 顶层键），既有调用方与判据不破。"""
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML

    one = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))[0]
    _c, reg = _reg(tmp_path)
    out = reg.invoke("mark_read", arxiv_id=one.arxiv_id, read=True)
    assert out["ok"] and out["arxiv_id"] == one.arxiv_id and out["read"] is True


def test_w9_bulk_star(tmp_path):
    """批量收藏：逐篇翻面回执；全坏 ⇒ 响亮 not_found。"""
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML

    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    _c, reg = _reg(tmp_path)
    ids = [papers[0].arxiv_id, papers[1].arxiv_id]
    out = reg.invoke("star_paper", arxiv_id=",".join(ids))
    assert out["ok"] and len(out["starred"]) == 2
    bad = reg.invoke("star_paper", arxiv_id="0000.00001,0000.00002")
    assert bad["ok"] is False and bad["error"]["kind"] == "not_found"


# ---------------------------------------------------------------- 定时任务已删除
def test_scheduler_is_gone():
    """删除守卫（能红：模块若被复活/改名回归，本判据红）：调度器与配置字段不复存在。"""
    assert importlib.util.find_spec("paperpilot.app.scheduler") is None
    from paperpilot.config import Settings

    assert "schedule" not in Settings.model_fields
    import paperpilot.app.cli as cli_mod

    src = cli_mod.__file__
    text = open(src, encoding="utf-8").read()
    assert "start_scheduler" not in text


# ---------------------------------------------------------------- W1：reason 带计数
def test_w1_empty_reason_carries_status_counts(tmp_path):
    """能红：空池诊断的 message 里带 archived/in_briefing 具体数字（不止字段名）。"""
    _c, reg = _reg(tmp_path)
    date_str, _cands = _drain_pool(reg)
    out = reg.invoke("prepare_review", date=date_str)
    assert out["ok"] is False and out["error"]["kind"] == "empty_pool"
    msg = out["error"]["message"]
    assert "archived=" in msg and "in_briefing=" in msg, msg


# ---------------------------------------------------------------- W7 自助调主题
def test_w7_update_topic_partial_fields_yaml_single_source(tmp_path):
    """能红：只改传入字段（动 quota 不动 keywords），写回 YAML 重载可见；未知名响亮带 suggest。"""
    _c, reg = _reg(tmp_path)
    first = _c.settings.topics[0]
    kws_before = list(first.keywords)
    out = reg.invoke("update_topic", name=first.name, quota=7,
                     authors="陈丞, Lukin", reason="测试调配额+作者")
    assert out["ok"] and set(out["changed"]) == {"quota", "authors"}, out
    assert first.quota == 7 and first.keywords == kws_before   # 未传的字段不动
    from paperpilot.config import load_settings
    reloaded = {t.name: t for t in load_settings(_c.settings.config_path).topics}
    assert reloaded[first.name].quota == 7
    assert reloaded[first.name].authors == ["陈丞", "Lukin"]    # YAML 唯一事实源
    bad = reg.invoke("update_topic", name="没有这个主题", quota=3)
    assert bad["ok"] is False and bad["error"]["kind"] == "unknown_topic"
    assert first.name in bad["error"]["suggest"]              # 可教学：现有主题名列出


def test_w7_update_topic_no_op_fails_loud(tmp_path):
    """不误报：一个字段都没改（全省略/负数哨兵）⇒ 响亮 no_fields，**不静默落盘**。"""
    _c, reg = _reg(tmp_path)
    out = reg.invoke("update_topic", name=_c.settings.topics[0].name)
    assert out["ok"] is False and out["error"]["kind"] == "no_fields"
    neg = reg.invoke("update_topic", name=_c.settings.topics[0].name,
                     quota=-1, threshold=-1.0)
    assert neg["ok"] is False and neg["error"]["kind"] == "no_fields"


# ---------------------------------------------------------------- W5 两阶段评审
def _candidates(reg, **kw):
    out = reg.invoke("prepare_review", **kw)
    assert out["ok"], out
    return out


def _payload_bytes(cands: list) -> int:
    import json
    return len(json.dumps(cands, ensure_ascii=False).encode("utf-8"))


def test_w5_brief_is_much_smaller_than_full(tmp_path):
    """能红：brief 阶段候选包 ≤ full 的 40%（真字节计数，不是"接入了"就算过）。"""
    _c, reg = _reg(tmp_path)
    full = _candidates(reg)
    brief = _candidates(reg, stage="brief")
    assert full["candidates"] and brief["candidates"]
    fb, bb = _payload_bytes(full["candidates"]), _payload_bytes(brief["candidates"])
    # 样例摘要本就短（多数 <300），省幅按语料浮动；机制层真上限另断言。
    # 真语料（arXiv 长摘）下 brief/full ≈ 0.4——那才是 W5 的省钱场景。
    assert bb <= fb * 0.95, f"brief={bb}B full={fb}B 没省"
    assert all(len(c["abstract"]) <= 300 for c in brief["candidates"])
    long_full = [c for c in full["candidates"] if len(c["abstract"]) > 300]
    for c in long_full:  # 截断机制逐篇对账
        assert c["abstract"][:300] == next(
            b["abstract"] for b in brief["candidates"] if b["arxiv_id"] == c["arxiv_id"])
    assert brief["stage"] == "brief" and full["stage"] == "full"


def test_w5_shortlist_view_but_file_stays_full(tmp_path):
    """能红：full+arxiv_ids 回执只装 shortlist；**存储仍全量**（submit 按全量校验、
    未入选者可被 submit 也可回落基线）。"""
    _c, reg = _reg(tmp_path)
    full = _candidates(reg)
    ids = [full["candidates"][0]["arxiv_id"], full["candidates"][1]["arxiv_id"]]
    picked = _candidates(reg, stage="full", arxiv_ids=",".join(ids))
    assert [c["arxiv_id"] for c in picked["candidates"]] == ids
    out = reg.invoke("submit_review", reviews=[
        {"arxiv_id": ids[0], "score": 0.8, "label": "worth", "reason": "r"}])
    assert out["ok"] and out["accepted"] == 1          # 全量 stored 校验生效


def test_w5_default_stage_unchanged_and_bad_stage_loud(tmp_path):
    """不误报：不传 stage = 旧行为（全量全文回执）；乱传 stage 响亮 bad_params。"""
    _c, reg = _reg(tmp_path)
    out = _candidates(reg)
    assert _payload_bytes(out["candidates"]) == _payload_bytes(
        _candidates(reg, stage="full")["candidates"])
    bad = reg.invoke("prepare_review", stage="mega")
    assert bad["ok"] is False and bad["error"]["kind"] == "bad_params"


# ---------------------------------------------------------------- W6 抓取收窄
def test_w6_fallback_is_keyword_intersection(tmp_path):
    """能红：兜底查询变成 (分类) AND (主题关键词并集)；关掉开关则只剩主题查询。"""
    from paperpilot.config import TopicCfg
    from paperpilot.infra.arxiv import build_queries

    topics = [TopicCfg(name="t", categories=["cs.CL"], keywords=["rag"])]
    qs = build_queries(topics, categories=["cs.AI"])
    assert len(qs) == 2
    fallback = qs[1]
    assert "cat:cs.AI" in fallback and 'all:"rag"' in fallback   # 不再无脑灌泛 AI
    assert len(build_queries(topics, categories=["cs.AI"], global_fallback=False)) == 1


def test_w6_settings_default_and_no_keywords_fallback(tmp_path):
    """不误报：默认开关为 True；主题全无关键词时兜底退回旧"分类 OR"行为（不造空 AND）。"""
    from paperpilot.config import Settings, TopicCfg
    from paperpilot.infra.arxiv import build_queries

    assert Settings().fetch_global_fallback is True
    qs = build_queries([TopicCfg(name="t", categories=["cs.CL"], keywords=[])],
                       categories=["cs.AI"])
    assert qs and "AND" not in qs[-1]                            # 退回纯分类 OR


# ---------------------------------------------- 同日两版日报：一天只认一行（实测 bug）
def test_same_day_two_finalizations_read_as_one_briefing(tmp_path):
    """能红：同日重跑定稿（旧行 superseded）⇒ 列表/日期高亮只认一条；删除删整天；
    undo 回滚的是当时那份（latest）。不误报：别的日期不受影响。"""
    _c, reg = _reg(tmp_path)
    repo = _c.repo

    def mk(markdown: str) -> None:
        repo.save_briefing(date="2098-01-02", run_id=f"r-{markdown}", title="T",
                           markdown=markdown, stats={"items": []}, ai_enabled=False,
                           actor="test", reason="同日二版")
        repo.save_briefing(date="2098-01-03", run_id="other", title="T2",
                           markdown="# 别的一天", stats={"items": []},
                           ai_enabled=False, actor="test", reason="对照日")

    mk("# v1")
    mk("# v2")
    same_day = [b for b in repo.briefings(limit=30) if b.date == "2098-01-02"]
    assert len(same_day) == 1, f"同日冒出 {len(same_day)} 行：高亮/删除都会双打"
    dates = [b.date for b in repo.briefings(limit=30)]
    assert len(dates) == len(set(dates)), "日期列表重复——chip 会一起亮"

    out = reg.invoke("delete_briefing", date="2098-01-02")
    assert out["ok"] and out["deleted"] == 2        # 一把删整天（含历史行），语义自洽
    assert [b.date for b in repo.briefings(limit=30)] == ["2098-01-03"]   # 对照日不动
    und = reg.invoke("undo", seq=0)
    assert und["ok"] and und.get("op") == "delete_briefing"
    back = [b for b in repo.briefings(limit=30) if b.date == "2098-01-02"]
    assert len(back) == 1 and back[0].markdown == "# v2"   # 回滚的是当时那份


if __name__ == "__main__":        # 方便单跑
    raise SystemExit(pytest.main([__file__, "-q"]))
