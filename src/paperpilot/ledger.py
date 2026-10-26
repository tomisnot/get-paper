"""事件账：唯一日志，mecha `LogSink` 的实现。

为什么账住在同一张 SQLite 里（而不是框架出厂的 JSONL）：**状态和账必须同库同事务**，
否则会出现"状态改了、账没记"或反过来。这里既是 mecha 的 History 落地，
也是项目自己的账本（信号、扫描、投递），两边共用同一个 seq 序列。

记录形状就是 mecha 的 `Event`：`seq / kind / op / target / after / before / actor /
reason / call_id / undoable / operation_id / role`。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from . import db


class EventLog:
    """只追加的事件表。`append` 是唯一写口。"""

    def __init__(self, conn):
        self.conn = conn

    # ---------------------------------------------------------------- 写
    def append(self, record: dict) -> int:
        """写一条（mecha LogSink.append）。返回落库后的 seq。"""
        cur = self.conn.execute(
            "INSERT INTO events (ts, kind, op, target, after, before, actor, reason,"
            " call_id, undoable, operation_id, role)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(record.get("ts") or db.now_iso()),
                str(record.get("kind") or "history"),
                str(record.get("op") or ""),
                str(record.get("target") or ""),
                _dump(record.get("after")),
                _dump(record.get("before")),
                str(record.get("actor") or ""),
                str(record.get("reason") or ""),
                str(record.get("call_id") or ""),
                _flag(record.get("undoable")),
                str(record.get("operation_id") or ""),
                str(record.get("role") or "action"),
            ))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def precheck(self, record: dict) -> None:
        """落地之前的可预见失败：值必须可 JSON 序列化。"""
        for key in ("after", "before"):
            value = record.get(key)
            if value is None:
                continue
            try:
                json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"事件 {key} 不可序列化：{exc}") from exc

    # ---------------------------------------------------------------- 读
    def since(self, seq: int = 0, *, actor: str | None = None, op: str | None = None,
              limit: int | None = None) -> Iterable[dict]:
        where, args = ["seq > ?"], [int(seq)]
        if actor:
            where.append("actor = ?")
            args.append(actor)
        if op:
            where.append("op LIKE ?")
            args.append(f"{op}%")
        sql = f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY seq"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        return [self.to_record(r) for r in rows]

    def last_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM events").fetchone()
        return int(row["s"])

    def recent(self, *, limit: int = 100, op_prefix: str = "", actor: str = "",
               target: str = "", since_seq: int = 0) -> list[dict]:
        where, args = ["seq > ?"], [int(since_seq)]
        if op_prefix:
            where.append("op LIKE ?")
            args.append(f"{op_prefix}%")
        if actor:
            where.append("actor = ?")
            args.append(actor)
        if target:
            where.append("target = ?")
            args.append(target)
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
            (*args, int(limit))).fetchall()
        return [self.to_record(r) for r in rows]

    @staticmethod
    def to_record(row) -> dict:
        return {
            "seq": int(row["seq"]), "ts": row["ts"], "kind": row["kind"],
            "op": row["op"], "target": row["target"],
            "after": _load(row["after"]), "before": _load(row["before"]),
            "actor": row["actor"], "reason": row["reason"], "call_id": row["call_id"],
            "undoable": None if row["undoable"] is None else bool(row["undoable"]),
            "operation_id": row["operation_id"], "role": row["role"],
        }


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(text) -> Any:
    if text is None or text == "":
        return None
    if isinstance(text, (int, float, bool)):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _flag(value) -> int | None:
    """三态：True/False/None（None = 未声明，不是"不可撤"）。"""
    if value is None:
        return None
    return 1 if value else 0
