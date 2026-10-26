"""SQLite 底座：连接、建表、时间工具。

设计约束（见 docs/设计-总纲.md §4）：
- 所有从属表挂 paper_id，而 papers 永不被批量删除 ⇒ 不需要级联删除策略；
- 草稿只存 arxiv_id，不存 paper_id；
- events 是唯一日志，只追加。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    arxiv_id         TEXT    NOT NULL UNIQUE,
    version          TEXT    NOT NULL DEFAULT '',
    title            TEXT    NOT NULL DEFAULT '',
    abstract         TEXT    NOT NULL DEFAULT '',
    authors          TEXT    NOT NULL DEFAULT '[]',
    categories       TEXT    NOT NULL DEFAULT '[]',
    primary_category TEXT    NOT NULL DEFAULT '',
    published_at     TEXT,
    updated_at       TEXT,
    pdf_url          TEXT    NOT NULL DEFAULT '',
    abs_url          TEXT    NOT NULL DEFAULT '',
    found_by         TEXT    NOT NULL DEFAULT '',
    first_seen_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_papers_published ON papers(published_at DESC);
CREATE INDEX IF NOT EXISTS ix_papers_primary   ON papers(primary_category);

CREATE TABLE IF NOT EXISTS cards (
    paper_id   INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    tldr       TEXT NOT NULL DEFAULT '',
    problem    TEXT NOT NULL DEFAULT '',
    method     TEXT NOT NULL DEFAULT '',
    results    TEXT NOT NULL DEFAULT '',
    novelty    TEXT NOT NULL DEFAULT '',
    terms      TEXT NOT NULL DEFAULT '[]',
    model      TEXT NOT NULL DEFAULT '',
    written_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    paper_id   INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    read       INTEGER NOT NULL DEFAULT 0,
    starred    INTEGER NOT NULL DEFAULT 0,
    skipped    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notes_paper ON notes(paper_id);

CREATE TABLE IF NOT EXISTS marks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    block_id   TEXT    NOT NULL DEFAULT '',
    quote      TEXT    NOT NULL DEFAULT '',
    body       TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL DEFAULT 'highlight',
    color      TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'active',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_marks_paper ON marks(paper_id);

CREATE TABLE IF NOT EXISTS paper_html (
    paper_id   INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'ok',
    path       TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('linear', 'network')),
    title      TEXT NOT NULL UNIQUE,
    note       TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}',
    author     TEXT NOT NULL DEFAULT 'ai',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'history',
    op           TEXT NOT NULL,
    target       TEXT NOT NULL DEFAULT '',
    after        TEXT,
    before       TEXT,
    actor        TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    call_id      TEXT NOT NULL DEFAULT '',
    undoable     INTEGER,
    operation_id TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'action'
);
CREATE INDEX IF NOT EXISTS ix_events_op   ON events(op);
CREATE INDEX IF NOT EXISTS ix_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_tgt  ON events(target);
CREATE INDEX IF NOT EXISTS ix_events_opid ON events(operation_id);

CREATE TABLE IF NOT EXISTS profile_weights (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    w          REAL NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'behavior',
    hits       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS citation_edges (
    src           TEXT NOT NULL,
    dst           TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'cites',
    dst_title     TEXT NOT NULL DEFAULT '',
    year          INTEGER NOT NULL DEFAULT 0,
    citations     INTEGER NOT NULL DEFAULT 0,
    influential   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON citation_edges(dst);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_calls (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    tokens  INTEGER NOT NULL DEFAULT 0,
    cost    REAL    NOT NULL DEFAULT 0
);
"""


def now_iso() -> str:
    """统一的落库时标：UTC、微秒精度、可字符串比较。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="microseconds")


def parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def days_ago_iso(days: float) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=float(days))).isoformat(sep=" ", timespec="microseconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return LockedConnection(conn)


class LockedConnection:
    """一个连接、多线程访问（FastAPI 的同步端点跑在线程池里）。

    只把 `execute` / `commit` 这类会碰 SQLite 状态的方法串行化，
    其余属性透传——够用，且不必让每个调用点都记得加锁。
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, *args, **kwargs):
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._lock:
            return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def init_db(conn: sqlite3.Connection) -> None:
    _migrate_events(conn)
    conn.executescript(SCHEMA)
    _finish_events_migration(conn)
    conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _migrate_events(conn: sqlite3.Connection) -> None:
    """旧形状的 events（没有 operation_id）先改名让路，新表建好后再搬过去。

    只在形状不符时动手；形状已对就什么都不做（幂等）。
    """
    cols = _columns(conn, "events")
    if not cols or "operation_id" in cols:
        return
    conn.execute("DROP TABLE IF EXISTS events_legacy")
    conn.execute("ALTER TABLE events RENAME TO events_legacy")
    conn.commit()


def _finish_events_migration(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "events_legacy")
    if not cols:
        return

    def pick(*names: str, default: str = "''") -> str:
        for name in names:
            if name in cols:
                return name
        return default

    after = pick("after", "payload", default="NULL")
    before = pick("before", default="NULL")
    kind = pick("kind", default="'history'")
    reason = pick("reason", default="''")
    conn.execute(
        "INSERT INTO events (seq, ts, kind, op, target, after, before, actor, reason,"
        " call_id, undoable, operation_id, role)"
        f" SELECT seq, ts, {kind}, op, target, {after}, {before}, actor, {reason},"
        " '', NULL, '', 'action' FROM events_legacy ORDER BY seq")
    conn.execute("DROP TABLE events_legacy")
    conn.commit()


def open_db(db_path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    conn = connect(db_path)
    if create:
        init_db(conn)
    return conn


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(text: str, default=None):
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except ValueError:
        return {} if default is None else default
