"""归因（P0-A）+ 记录仪（P0-B）+ undo（P1）+ 可教学兜底（P2）测试。

对应 docs/GAPS.md §7 的三条判据：
  ① 每条写入带 actor/reason；
  ② events 只增不改（UPDATE/DELETE 必须失败——DB 触发器钉死）；
  ③ 可逆 op 的 undo 往返一致；不可逆 op 明确拒绝。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from paperpilot.infra.arxiv import parse_atom
from paperpilot.infra.orm import Event

from .conftest import SAMPLE_XML


@pytest.fixture
def loaded(repo):
    repo.sync_topics([])
    repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")))
    return repo


def _all_events(repo) -> list[Event]:
    with repo.sf() as s:
        return list(s.scalars(text_event_query()))


def _ops(repo, op: str) -> list[Event]:
    return [e for e in _all_events(repo) if e.op == op]


def text_event_query():
    from sqlalchemy import select

    return select(Event).order_by(Event.seq)


# ---------------------------------------------------------------- 判据①：写入必带归因
def test_writes_carry_actor_and_reason(loaded):
    paper = loaded.get_paper("2608.01101")

    loaded.set_read(paper, read=True, actor="ai", reason="AI 读过了")
    loaded.toggle_star(paper, actor="human", reason="收藏")
    loaded.add_note(paper, "值得精读", actor="ai", reason="调研笔记")

    events = _all_events(loaded)
    ops = {e.op for e in events}
    assert {"set_read", "star_paper", "add_note"} <= ops

    for e in events:
        assert e.actor, f"事件 #{e.seq}（{e.op}）缺 actor"
        # reason 可空（内部流程），但调用方给了就必须落
    actors = {e.op: e.actor for e in events if e.op in ("set_read", "star_paper", "add_note")}
    assert actors["set_read"] == "ai"
    assert actors["star_paper"] == "human"
    reasons = {e.op: e.reason for e in events if e.op in ("set_read", "star_paper", "add_note")}
    assert reasons["set_read"] == "AI 读过了"
    assert reasons["add_note"] == "调研笔记"


def test_events_record_before_after_snapshots(loaded):
    paper = loaded.get_paper("2608.01101")
    loaded.toggle_star(paper, actor="ai", reason="第一次收藏")

    event = [e for e in _all_events(loaded) if e.op == "star_paper"][0]
    assert event.before["star"] is False
    assert event.after["star"] is True
    assert event.after["arxiv_id"] == "2608.01101"


def test_pipeline_events_have_actor(loaded, pipeline):
    result = pipeline.run(force=True, actor="ai", reason="AI 跑全流程")
    assert result.error is None
    ops = {e.op for e in _all_events(loaded)}
    assert "save_briefing" in ops
    briefing_event = [e for e in _all_events(loaded) if e.op == "save_briefing"][0]
    assert briefing_event.actor == "ai"
    assert briefing_event.reversible == 0  # 定稿不可逆
    assert briefing_event.after["date"] == result.date


# ---------------------------------------------------------------- 判据②：events 只增不改
def test_events_are_append_only(loaded):
    paper = loaded.get_paper("2608.01101")
    loaded.toggle_star(paper, actor="ai", reason="造一条事件")

    with loaded.sf() as s:
        event = s.scalars(text_event_query()).first()
        assert event is not None
        # UPDATE 必须被触发器 ABORT
        with pytest.raises(SQLAlchemyError):
            s.execute(
                text("UPDATE events SET reason = '篡改' WHERE seq = :seq"),
                {"seq": event.seq},
            )
            s.commit()
        # DELETE 必须被触发器 ABORT
        with pytest.raises(SQLAlchemyError):
            s.execute(text("DELETE FROM events WHERE seq = :seq"), {"seq": event.seq})
            s.commit()
        s.rollback()

    # 原样还在（按 op 取，避开 fixture 的入库事件）
    star_events = _ops(loaded, "star_paper")
    assert star_events and star_events[0].reason == "造一条事件"


# ---------------------------------------------------------------- 判据③：undo 往返一致
def test_undo_reading_state_roundtrip(loaded):
    paper = loaded.get_paper("2608.01101")
    before = loaded.get_paper("2608.01101").reading
    before_triple = (
        (before.read, before.star, before.marked_skip) if before else (False, False, False)
    )

    loaded.toggle_star(paper, actor="ai", reason="AI 收藏")
    assert loaded.get_paper("2608.01101").reading.star is True

    result = loaded.undo(0, actor="ai", reason="撤销误收藏")
    assert result["ok"] and result["op"] == "star_paper"

    after = loaded.get_paper("2608.01101").reading
    after_triple = (after.read, after.star, after.marked_skip)
    assert after_triple == before_triple, "undo 后阅读态应回到操作前"

    # undo 本身也记一条事件（可追溯）
    ops = [e.op for e in _all_events(loaded)]
    assert ops[-1] == "undo"


def test_undo_add_note_removes_it(loaded):
    paper = loaded.get_paper("2608.01101")
    loaded.add_note(paper, "AI 误加的笔记", actor="ai", reason="误加")
    assert len(loaded.get_paper("2608.01101").notes) == 1

    loaded.undo(0, actor="ai", reason="撤销误加笔记")
    assert loaded.get_paper("2608.01101").notes == []


def test_undo_specific_seq(loaded):
    paper = loaded.get_paper("2608.01101")
    loaded.toggle_star(paper, actor="ai", reason="收藏1")
    loaded.toggle_star(paper, actor="ai", reason="收藏2")  # 又取消
    events = [e for e in _all_events(loaded) if e.op == "star_paper"]
    first_seq = events[0].seq

    result = loaded.undo(first_seq, actor="human", reason="撤销第一条")
    assert result["undone_seq"] == first_seq
    # 第一条的 before 是 star=False → 撤销后应为 False
    assert loaded.get_paper("2608.01101").reading.star is False


def test_undo_irreversible_refuses_with_teachable_error(loaded, pipeline):
    pipeline.run(force=True, actor="ai", reason="定稿")
    event = [e for e in _all_events(loaded) if e.op == "save_briefing"][0]

    with pytest.raises(Exception) as exc_info:
        loaded.undo(event.seq, actor="ai", reason="想撤定稿")
    err = exc_info.value
    assert getattr(err, "kind", None) == "irreversible"
    assert "重跑" in (getattr(err, "hint", "") or "")


def test_undo_nothing_to_undo_is_teachable(loaded):
    with pytest.raises(Exception) as exc_info:
        loaded.undo(0, actor="ai", reason="空撤")
    assert getattr(exc_info.value, "kind", None) == "nothing_to_undo"
    assert "get_activity" in (getattr(exc_info.value, "hint", "") or "")


# ---------------------------------------------------------------- events 读面
def test_events_since_filters(loaded):
    paper = loaded.get_paper("2608.01101")
    loaded.toggle_star(paper, actor="ai", reason="AI 收藏")
    loaded.add_note(paper, "笔记", actor="human", reason="人加笔记")

    # 只数本测试自己写的两条（fixture 的入库事件除外）
    star_events = loaded.events_since(op="star_paper", limit=50)
    assert star_events["count"] == 1
    note_events = loaded.events_since(op="add_note", limit=50)
    assert note_events["count"] == 1
    assert note_events["events"][0]["actor"] == "human"

    all_events = loaded.events_since(limit=50)
    assert all_events["count"] == 3  # 入库 + 收藏 + 笔记

    ai_only = loaded.events_since(actor="ai", limit=50)
    assert ai_only["count"] == 1 and ai_only["events"][0]["actor"] == "ai"

    # diff-since-seq：从 last_seq 之后没有新事件
    nothing = loaded.events_since(since_seq=all_events["last_seq"], limit=50)
    assert nothing["count"] == 0
