"""**账的落地点**：把 mecha 的账写进本仓的 `events` 表（"四仓共用一本账"的 GP 侧）。

## 为什么要这个（方向）
**账必须住在状态旁边**：GP 的域状态在 SQLite 里 ⇒ 只有**同库同事务**才能做到
"状态变了、账不可能没记"。⇒ 不是把 GP 的账搬进 mecha 的 JSONL，而是**让 mecha 把账写进 GP 的库**
（`mecha.assembly.assemble(log_sink=...)` 那个注入点）。

## 三件事说清
* **形状对齐**：mecha `Event` = `seq/kind/op/target/after/before/actor/reason/call_id/ts/undoable`
  ⇒ 本表列名与它**同名**（`kind`/`call_id` 是本轮新加的两列；`reversible` 已**改名** `undoable`）。
* **同事务**：`append()` 优先写进 `ledger_session(...)` 里那个在途 session（**跟着调用方一起 commit**）
  ⇒ mecha 完全不需要知道"事务"这回事（缝在本侧收口）。没有在途 session 时（如 `gate.set` 的配置写、
  命令审计）自己开一个 session 并提交。
* **一个序列**：`seq` **由数据库分配**（表内自增主键）⇒ "配置写 / 域写 / 命令审计"共用**同一个序列**。
  ⚠ mecha 传进来的 `seq` 是它**内存计数**的值，本落地点**不采信**（表内自增才是权威；
  进程重启后 `History._resume` 会从 `last_seq()` 重新续上）。

## `kind` 的语义（别与"是不是状态变更"混）
`kind` 判的是"**这条进不进框架那份配置快照**"（`fold` 只折 `kind=="state"`），**不是**"它是不是状态变更"。
GP 的域状态在**表里**、不由账折叠出来 ⇒ **GP 的域事件一律 `kind="history"`** ✓。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from contextvars import ContextVar

from sqlalchemy import func, select

from .orm import Event

#: 在途 session（同事务写口的缝）。由 `ledger_session(...)` 设置，`append` 读取。
_CURRENT_SESSION: ContextVar[object | None] = ContextVar("paperpilot_ledger_session", default=None)


@contextlib.contextmanager
def ledger_session(session):
    """块内的 `SqlLedger.append()` 都落进这个 session（**随调用方 commit**）。"""
    token = _CURRENT_SESSION.set(session)
    try:
        yield session
    finally:
        _CURRENT_SESSION.reset(token)


class SqlLedger:
    """实现 mecha ``LogSink``（``append`` / ``since`` / ``last_seq``）。"""

    def __init__(self, session_factory) -> None:
        self.sf = session_factory

    # ------------------------------------------------------------------ 写口
    @staticmethod
    def build_row(record: dict) -> Event:
        """账记录 → ORM 行（**唯一的构造处**；mecha 写口与域写口共用，防两份构造漂移）。"""
        return Event(
            kind=str(record.get("kind") or "history"),
            op=str(record.get("op") or ""),
            target=str(record.get("target") or ""),
            actor=str(record.get("actor") or "system"),
            reason=str(record.get("reason") or ""),
            call_id=str(record.get("call_id") or ""),
            # ⚠ `after`/`before` 可能是**标量**（配置键的值就是 1/"x"）⇒ 原样落 JSON 列，**不许包一层**
            # （包了就把值改坏：undo 与前像都会失真）。
            before=record.get("before"),
            after=record.get("after"),
            undoable=record.get("undoable"),
        )

    def append(self, record: dict) -> Event:
        """落一条账。在途 session 里就加进去（同事务），否则自开一个 session 提交。"""
        row = self.build_row(record)
        pending = _CURRENT_SESSION.get()
        if pending is not None:
            pending.add(row)
            return row
        with self.sf() as own:
            own.add(row)
            own.commit()
            own.refresh(row)
            return row

    # ------------------------------------------------------------------ 读口
    @staticmethod
    def _record(e: Event) -> dict:
        """ORM 行 → mecha 账记录形状（键名与 mecha `Event.to_record()` 一致）。"""
        return {
            "seq": e.seq,
            "kind": e.kind,
            "op": e.op,
            "target": e.target,
            "after": e.after,
            "before": e.before,
            "actor": e.actor,
            "reason": e.reason,
            "call_id": e.call_id,
            "ts": e.ts.isoformat() if e.ts else "",
            "undoable": e.undoable,
        }

    def since(self, seq: int, *, actor: str | None = None, op: str | None = None,
              limit: int | None = None) -> Iterable[dict]:
        """按史序**升序**读 `seq > since`（`History._resume` 依赖升序）。"""
        with self.sf() as s:
            stmt = select(Event)
            if seq:
                stmt = stmt.where(Event.seq > int(seq))
            if actor:
                stmt = stmt.where(Event.actor == actor)
            if op:
                stmt = stmt.where(Event.op == op)
            stmt = stmt.order_by(Event.seq.asc())
            if limit:
                stmt = stmt.limit(int(limit))
            return [self._record(e) for e in s.scalars(stmt)]

    def last_seq(self) -> int:
        with self.sf() as s:
            return int(s.scalar(select(func.max(Event.seq))) or 0)
