"""AI 调用体验打磨 EXP-1（止血）判据 —— 每条配「能红 + 不许误报」。

对应 docs/AI-EXPERIENCE-PLAN-2026-09-26.md 的 N1–N6、N9：
E1.1 单字段不击穿整批 · E1.2 review_status 标准信封 + 桥接 bad_envelope 兜底 ·
E1.3 信封纪律守卫（无裸 ok:false） · E1.4 read_authority 自省 · E1.5 三段 date 对称 ·
E1.6 label 归一化+enum · E1.7 评审输入豁免体积截断。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from mecha.authority import Mode

from paperpilot.app.container import build_container
from paperpilot.capabilities import registry_for
from paperpilot.capabilities.base import gate as volume_gate
from paperpilot.infra.arxiv import parse_atom
from paperpilot.mecha_adapter.tools import envelope_to_error

from .conftest import SAMPLE_XML, make_settings
from .test_mecha_adapter import EXPECTED_TOOLS, _call, _stack


def _reg(tmp_path):
    container = build_container(make_settings(tmp_path / "data"))
    container.repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")))
    return container, registry_for(container)


def _prepared_cands(reg):
    prepared = reg.invoke("prepare_review")
    assert prepared["ok"] and prepared["candidates"], "prepare_review 应给出候选"
    return prepared["date"], prepared["candidates"]


# ---------------------------------------------------------------- E1.1 (N1)
def test_submit_review_one_bad_item_does_not_kill_the_batch(tmp_path):
    """能红：20 篇里 1 篇缺 score ⇒ 其余照常落盘（accepted=N-1），坏的那进 rejected。

    旧行为是 `item["score"]` 抛 KeyError 击穿整批、accepted 全丢；修后 per-item fail。
    """
    _container, reg = _reg(tmp_path)
    _date, cands = _prepared_cands(reg)
    reviews = [{"arxiv_id": c["arxiv_id"], "score": 0.9, "label": "worth", "reason": "r"}
               for c in cands]
    reviews[0] = {k: v for k, v in reviews[0].items() if k != "score"}   # 故意抽掉 score
    out = reg.invoke("submit_review", reviews=reviews)                    # date 省略（E1.5）
    assert out["ok"] is True
    assert out["accepted"] == len(reviews) - 1
    assert len(out["rejected"]) == 1 and "score" in out["rejected"][0]["why"]


def test_submit_review_all_valid_no_false_reject(tmp_path):
    """不许误报：全合法 ⇒ 全 accepted、rejected 空。"""
    _container, reg = _reg(tmp_path)
    _date, cands = _prepared_cands(reg)
    reviews = [{"arxiv_id": c["arxiv_id"], "score": 0.8, "label": "worth", "reason": "r"}
               for c in cands]
    out = reg.invoke("submit_review", reviews=reviews)
    assert out["ok"] and out["accepted"] == len(reviews) and out["rejected"] == []


# ---------------------------------------------------------------- E1.5 (N6)
def test_submit_review_date_defaults_to_today(tmp_path):
    """三段 date 对称：submit_review 不传 date ⇒ 落今天（与 prepare/finalize 一致）。"""
    _container, reg = _reg(tmp_path)
    _date, cands = _prepared_cands(reg)
    out = reg.invoke("submit_review",
                     reviews=[{"arxiv_id": cands[0]["arxiv_id"], "score": 0.7,
                               "label": "worth", "reason": "r"}])
    assert out["ok"] and out["date"] == date.today().isoformat()


# ---------------------------------------------------------------- E1.6 (N9)
def test_label_normalized_and_illegal_rejected(tmp_path):
    """尾空格/大小写的合法 label 归一后仍收；非法 label（"必读"）进 rejected 并教学。"""
    _container, reg = _reg(tmp_path)
    _date, cands = _prepared_cands(reg)
    reviews = [
        {"arxiv_id": cands[0]["arxiv_id"], "score": 0.9, "label": "must_read ", "reason": "r"},
        {"arxiv_id": cands[1]["arxiv_id"], "score": 0.9, "label": "必读", "reason": "r"},
    ]
    out = reg.invoke("submit_review", reviews=reviews)
    assert out["accepted"] == 1                                  # "must_read " 归一后收下
    assert len(out["rejected"]) == 1
    assert "label" in out["rejected"][0]["why"] and "必读" in out["rejected"][0]["why"]


# ---------------------------------------------------------------- E1.2 (N2)
def test_review_status_failure_is_standard_envelope(tmp_path):
    """未 prepare 的日期：review_status 返回**标准 error**（hint 指向 prepare_review）。"""
    _container, reg = _reg(tmp_path)
    out = reg.invoke("review_status", date="2000-01-01")
    assert out["ok"] is False
    assert out["error"]["kind"] == "no_review"
    assert "prepare_review" in out["error"]["hint"]


def test_bridge_flags_bare_ok_false_as_bad_envelope():
    """能红（桥接兜底）：裸 ok:false（无 error）不被吞成"调用失败"，而是 bad_envelope + 透传原文。"""
    err = envelope_to_error({"ok": False, "status": "none", "date": "2026-09-26"})
    assert err.kind == "bad_envelope"
    assert "status" in err.message and "none" in err.message   # 原始 payload 片段没丢


# ---------------------------------------------------------------- E1.3 信封纪律守卫
def test_no_bare_ok_false_across_failure_paths(tmp_path):
    """守卫：各失败路径的 `ok:false` **必带 `error{kind}`**（N2 一类不复发）。"""
    _container, reg = _reg(tmp_path)
    failures = [
        reg.invoke("get_paper", arxiv_id="0000.00000"),
        reg.invoke("review_status", date="2000-01-01"),
        reg.invoke("undo", seq=9_999_999),
        reg.invoke("set_topic_enabled", name="不存在", enabled=True),
    ]
    checked = 0
    for env in failures:
        assert env.get("ok") is False, f"预期失败但返 ok=true：{env}"
        assert isinstance(env.get("error"), dict) and env["error"].get("kind"), \
            f"裸 ok:false（违反信封纪律）：{env}"
        checked += 1
    assert checked == len(failures)          # R8：确实扫到了失败样本，非空表假绿


# ---------------------------------------------------------------- E1.4 (N5) read_authority
def test_read_authority_self_reflection_and_hint_pointer(tmp_path):
    """写权自省：LOCKED 下 ai_can_write=false + how_to_open；写被拒 hint 引用 read_authority。"""
    _container, stack = _stack(tmp_path, open_ai=False)         # 出厂 LOCKED
    tools = stack["tools"]
    assert "read_authority" in EXPECTED_TOOLS
    ra = _call(tools, "read_authority")
    assert ra["ok"] and ra["mode"] == "locked"
    assert ra["ai_can_write"] is False and ra["how_to_open"]
    # 写被拒 → hint 指向 read_authority（先查后写）
    denied = _call(tools, "add_note", arxiv_id="2608.01101", content="x")
    assert denied["ok"] is False and "read_authority" in denied["error"]["hint"]
    # 开闸后 AI 可写
    stack["authority"].switch_mode(Mode.AI, side="human")
    assert _call(tools, "read_authority")["ai_can_write"] is True


# ---------------------------------------------------------------- E1.7 (N3) 评审输入豁免截断
def test_volume_gate_exempts_candidates_still_bounds_others():
    """candidates 不再被截到 20（评审全集送达）；其它长列表仍截（不误放宽）。"""
    gated_cands = volume_gate({"ok": True, "candidates": [{"i": i} for i in range(40)]})
    assert len(gated_cands["candidates"]) == 40                 # 评审输入不砍
    assert "_candidates_truncated" not in gated_cands
    gated_papers = volume_gate({"ok": True, "papers": [{"i": i} for i in range(40)]})
    assert len(gated_papers["papers"]) == 20                    # 其它列表仍受 _LIST_KEEP
    assert "_papers_truncated" in gated_papers


def test_prepare_review_returns_full_candidate_set(tmp_path):
    """端到端：prepare_review 回程 candidates 数 == min(n_after_rules, 40)，无截断标记。"""
    _container, reg = _reg(tmp_path)
    _date, cands = _prepared_cands(reg)
    prepared = reg.invoke("prepare_review")
    assert prepared["n_after_rules"] >= 1
    assert len(prepared["candidates"]) == min(prepared["n_after_rules"], 40)
    assert "_candidates_truncated" not in prepared


# ---------------------------------------------------------------- E2.2 (缺口2) estimate_sec
def test_every_write_command_has_nonzero_estimate(tmp_path):
    """能红（E2.2）：每条写命令 estimate()>0（旧一律 0.0 则此判据红）。"""
    _c, stack = _stack(tmp_path)
    cmds = stack["commands"]
    assert stack["command_names"], "应有写命令"
    for name in stack["command_names"]:
        assert cmds.estimate(name, {}) > 0, f"命令 {name} 预估为 0（AI 无法据以决定是否 job 化）"


def test_estimate_callable_varies_with_args(tmp_path):
    """fetch/run 的预估是 callable，随参数变（红证：塑 0 常量 ⇒ 不等⇒红）。"""
    _c, stack = _stack(tmp_path)
    cmds = stack["commands"]
    assert cmds.estimate("fetch_papers", {"days": 1}) < cmds.estimate("fetch_papers", {"days": 30})
    assert cmds.estimate("run_pipeline", {}) > 0


# ---------------------------------------------------------------- E3.1 (N4) search 分页
def test_search_papers_offset_pages_are_disjoint(tmp_path):
    """limit/offset 翻页：两页 arxiv_id 零交集 + next_offset 可续取（旧无 offset ⇒ 红）。"""
    _c, reg = _reg(tmp_path)
    total = reg.invoke("search_papers", limit=20, offset=0)
    assert total["ok"]
    assert len(total["papers"]) >= 6, f"样本论文太少({len(total['papers'])})无法验证分页"
    a = reg.invoke("search_papers", limit=3, offset=0)
    b = reg.invoke("search_papers", limit=3, offset=3)
    ida = {p["arxiv_id"] for p in a["papers"]}
    idb = {p["arxiv_id"] for p in b["papers"]}
    assert len(ida) == 3 and len(idb) == 3 and ida.isdisjoint(idb)   # 翻页不重
    assert a["next_offset"] == 3 and b["offset"] == 3               # 游标可执行


# ---------------------------------------------------------------- E3.2 (N8) 描述单一来源
def test_capability_specs_carry_param_descriptions(tmp_path):
    """specs() 每个模型可见参数带 description（actor 由投影层隐去，不算）。"""
    _c, reg = _reg(tmp_path)
    checked = 0
    for s in reg.specs():
        for pname, info in s["params"].items():
            if pname == "actor":                      # 模型不可见，不需描述
                continue
            assert info.get("description"), f"{s['name']}.{pname} 缺 description（N8 未做全）"
            checked += 1
    assert checked >= 30, f"只扫到 {checked} 个带描述参数，描述表远不够全"


def test_param_description_map_drift_fails_loud(tmp_path):
    """描述表引用不存在的参数 ⇒ 响亮报错（D1/D4：两份不能漂）。"""
    container = build_container(make_settings(tmp_path / "data"))
    reg = registry_for(container)
    with pytest.raises(KeyError):
        reg.attach_param_descriptions({"get_paper": {"no_such_param": "x"}})


# ---------------------------------------------------------------- E2.1 (缺口1) jobs 长活
def test_submit_job_runs_pipeline_and_completes(tmp_path):
    """提交→阻塞到终态（无轮询竞态）→done 且真落了简报。"""
    container, stack = _stack(tmp_path, open_ai=True)
    tools = stack["tools"]
    sub = _call(tools, "submit_job", reason="测试后台跑批")
    assert sub["ok"] and sub["job_id"]
    st = stack["jobs"].wait(sub["job_id"], timeout=120)
    assert st["state"] == "done", st
    rd = _call(tools, "read_job", job_id=sub["job_id"])
    assert rd["ok"] and rd["state"] == "done" and "result" in rd
    assert container.repo.briefings(limit=5)          # 副作用真的发生了（产了简报）


def test_cancelled_pipeline_run_has_no_side_effect(tmp_path):
    """能红（取消后副作用不继续）：写库前有取消点 ⇒ 已取消的 run 不写简报。"""
    from mecha.surface import ExecutionContext
    container, stack = _stack(tmp_path, open_ai=True)
    ctx = ExecutionContext()
    ctx.request_cancel()
    res = stack["engine"].run({}, context=ctx)
    assert res.get("error", {}).get("kind") == "cancelled"
    assert not container.repo.briefings(limit=5)       # 未写任何简报


def test_submit_job_respects_project_concurrency_limit(tmp_path, monkeypatch):
    """项目兜并发上限（框架不排队）：超上限响亮拒绝 job_limit。"""
    _c, stack = _stack(tmp_path, open_ai=True)
    tools = stack["tools"]
    # fake submit：拿到许可但不跑 _work ⇒ 许可永不释放（确定性占位，不靠真流水线时长）
    monkeypatch.setattr(stack["jobs"], "submit",
                        lambda fn, **k: SimpleNamespace(id="job-fake",
                                                         state=SimpleNamespace(value="pending")))
    assert _call(tools, "submit_job")["ok"] is True          # 1/2
    assert _call(tools, "submit_job")["ok"] is True          # 2/2
    third = _call(tools, "submit_job")                       # 3rd ⇒ 超限
    assert third["ok"] is False and third["error"]["kind"] == "job_limit"


# ================================================================ R2 追加（2026-09-26）
# 用户第二轮实测反馈：prepare_review 空池静默 ok:true+[]、Web 无简报管理、
# cancel_job 已终态仍回“取消已登记”。前两个与 N1/N2 同族（diagnosability），后一个是
# proprioception。下面五条判据沿用“能红 + 不误报”对偶纪律。


def _drain_pool(reg):
    """把当前 new 池一轮消费完（prepare → submit → finalize force）。"""
    date_str, cands = _prepared_cands(reg)
    reviews = [{"arxiv_id": c["arxiv_id"], "score": 0.7,
                "label": "worth", "reason": "r"} for c in cands]
    reg.invoke("submit_review", date=date_str, reviews=reviews)
    fin = reg.invoke("finalize_briefing", date=date_str, force=True)
    assert fin["ok"], fin
    return date_str, cands


# ------------------------------------------------ R2-1 空池诊断（与 N1/N2 同族）
def test_prepare_review_empty_pool_returns_diagnostic_not_silent_ok(tmp_path):
    """能红：上一轮把池消费完 ⇒ 再 prepare 不再静默 ok:true+candidates:[]，
    而是回 err(kind=empty_pool) + hint 给出路（requeue/fetch_papers/lookback）。"""
    _c, reg = _reg(tmp_path)
    date_str, _cands = _drain_pool(reg)
    second = reg.invoke("prepare_review", date=date_str)
    assert second["ok"] is False, f"空池应响亮失败，实得：{second}"
    err = second["error"]
    assert err["kind"] == "empty_pool", err
    hint = err.get("hint", "")
    assert ("requeue" in hint or "fetch_papers" in hint
            or "lookback_days" in hint), hint


def test_prepare_review_no_false_diagnosis_when_pool_has_new(tmp_path):
    """不误报：池里还有 new ⇒ prepare 应正常 ok:true（不拿“empty”误伤）。"""
    _c, reg = _reg(tmp_path)
    out = reg.invoke("prepare_review")
    assert out["ok"] is True, f"非空池不应诊断：{out}"
    assert out.get("candidates"), "candidates 非空"


# --------------------------------------------------- R2-2 再审回炉 requeue
def test_prepare_review_requeue_flips_archived_back_to_new(tmp_path):
    """能红：空池后 requeue=True ⇒ reopen_by_status 把近 lookback 内 archived/in_briefing
    拉回 new，候选重新非空 + 回执带 requeue_flipped 计数。"""
    _c, reg = _reg(tmp_path)
    date_str, _cands = _drain_pool(reg)
    assert reg.invoke("prepare_review", date=date_str)["ok"] is False
    again = reg.invoke("prepare_review", date=date_str, requeue=True)
    assert again["ok"] is True, f"requeue 应能拉起候选：{again}"
    assert again.get("candidates"), "requeue 后候选非空"
    assert int(again.get("requeue_flipped") or 0) > 0


# ---------------------------------------- R2-3 删简报与 undo 回滚（Web 同构后端）
def test_delete_briefing_roundtrip_with_undo(tmp_path):
    """能红：delete_briefing 写可逆事件；undo(seq=0) 从 before 快照重建 markdown+stats。"""
    container, reg = _reg(tmp_path)
    repo = container.repo
    repo.save_briefing(
        date="2099-01-01", run_id="t-run", title="测试",
        markdown="# 测试正文", stats={"items": [{"arxiv_id": "x"}]},
        ai_enabled=False, actor="test", reason="测试建简报",
    )
    assert repo.briefing_for_date("2099-01-01") is not None
    out = reg.invoke("delete_briefing", date="2099-01-01", reason="测试删")
    assert out["ok"] and int(out.get("deleted") or 0) >= 1
    assert repo.briefing_for_date("2099-01-01") is None, "删后行应不在"
    und = reg.invoke("undo", seq=0, reason="测试撤销删除")
    assert und["ok"] is True, und
    assert und.get("op") == "delete_briefing", und
    restored = repo.briefing_for_date("2099-01-01")
    assert restored is not None and restored.markdown == "# 测试正文"


def test_delete_briefing_unknown_date_is_teaching_err(tmp_path):
    """不误报：删不存在的日期 ⇒ err(no_briefing) + hint 引 list_briefings，
    不静默 ok:true deleted=0。"""
    _c, reg = _reg(tmp_path)
    out = reg.invoke("delete_briefing", date="1999-01-01")
    assert out["ok"] is False, out
    assert out["error"]["kind"] == "no_briefing", out
    assert "list_briefings" in out["error"].get("hint", "")


# ------------------------------------------------- R2-4 cancel_job 已终态如实文案
def test_cancel_job_terminal_state_is_honestly_reported(tmp_path):
    """能红：对已 done 的 job 调 cancel_job ⇒ note 里说“无需取消”、不再说“取消已登记”。"""
    import time

    _c, stack = _stack(tmp_path, open_ai=True)
    tools = stack["tools"]

    def _noop(_ctx):
        return {"ok": True}

    job = stack["jobs"].submit(_noop, command="noop", cancel_supported=True)
    for _ in range(200):
        if stack["jobs"].get(job.id).state.value == "done":
            break
        time.sleep(0.02)
    assert stack["jobs"].get(job.id).state.value == "done"
    res = tools.execute("cancel_job", {"job_id": job.id})
    assert res["is_error"] is False, res
    note = res["value"].get("note", "")
    assert "无需取消" in note, note
    assert "取消已登记" not in note, f"对终态 job 不应仍说‘已登记’：{note}"
