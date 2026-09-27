"""N12/N13/N14/N15 判据：显式意图 > 启发式预筛；authors 上自描述面；gate 配置管到全段。

背景（用户两批实操反馈）：
- N12/N14：floor 把"按 id 点名要的论文"静默挡回——显式意图应胜启发式预筛；
  brief 粗筛段也应排序不过滤（救援权留给评审者）。
- N13：set 了 authors 只能翻 YAML 自证——list_topics 必须回 authors。
- N15：set_config 只写 gate 快照，finalize_briefing 却读容器冻结 settings——
  抬了 max_papers=80 实际仍按 12 封顶（silent no-op 比响亮拒绝更坏）。
  修法：命令面 handler 边界统一注入 gate 配置覆盖（prepare/submit/finalize 全吃到）。
"""

from __future__ import annotations

from .test_ai_experience import _reg
from .test_mecha_adapter import _call, _stack


def test_n12_n14_explicit_intent_beats_floor(tmp_path):
    """能红：floor 滤光全池时——默认 full 仍被拦（诊断）；brief 段（N14）排序不过滤，
    显式 arxiv_ids 点名（N12）绕过 floor，点名要的绝不静默丢。"""
    _c, reg = _reg(tmp_path)
    _c.settings.scoring.review_floor = 0.99
    blocked = reg.invoke("prepare_review")
    assert blocked["ok"] is False and blocked["error"]["kind"] == "empty_pool"

    br = reg.invoke("prepare_review", stage="brief")
    assert br["ok"] and br["candidates"], br
    assert br["floor_applied"]["applied"] is False
    scores = [c["baseline"]["score"] for c in br["candidates"]]
    assert scores == sorted(scores, reverse=True)          # 排序不过滤

    low = min(br["candidates"], key=lambda c: c["baseline"]["score"])["arxiv_id"]
    named = reg.invoke("prepare_review", arxiv_ids=low)    # N12：点名即胜
    assert named["ok"] and [c["arxiv_id"] for c in named["candidates"]] == [low]
    assert named["floor_applied"]["applied"] is False


def test_n13_list_topics_returns_authors(tmp_path):
    """能红：update_topic 设 authors 后 list_topics 直接可见；未设过的给 [] 而非缺键。"""
    _c, reg = _reg(tmp_path)
    name = _c.settings.topics[0].name
    out = reg.invoke("update_topic", name=name, authors="陈丞, Lukin")
    assert out["ok"], out
    topics = {t["name"]: t for t in reg.invoke("list_topics")["topics"]}
    assert topics[name]["authors"] == ["陈丞", "Lukin"]
    assert topics[_c.settings.topics[1].name]["authors"] == []


def test_n15_gate_config_authoritative_over_finalize(tmp_path):
    """能红（N15 主案）：set_config 抬 gate max_papers 后 finalize 真按 gate 封顶，
    且**共享 settings 一字不动**（覆盖只活在本次调用的线程里）。"""
    container, stack = _stack(tmp_path)
    tools = stack["tools"]
    yaml_max = container.settings.scoring.max_papers        # conftest：12
    out = _call(tools, "set_config", key="scoring.max_papers",
                value=2, reason="N15 验收：压顶到 2")
    assert out["ok"], out
    prep = _call(tools, "prepare_review")
    assert prep["ok"] and prep["candidates"]
    reviews = [{"arxiv_id": c["arxiv_id"], "score": 0.95, "label": "must_read",
                "reason": "全票必读"} for c in prep["candidates"]]
    sub = _call(tools, "submit_review", reviews=reviews)
    assert sub["ok"] and sub["accepted"] >= 3, sub           # 备得出 >2 的入选量
    fin = _call(tools, "finalize_briefing", force=True)
    assert fin["ok"], fin
    assert fin["selected"] <= 2, f"gate max_papers=2 没管住 finalize：{fin}"
    assert container.settings.scoring.max_papers == yaml_max  # 静不静污染共享态
