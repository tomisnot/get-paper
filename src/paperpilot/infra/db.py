"""数据库引擎与会话工厂（SQLite，单机个人使用）。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .orm import Base


def make_engine(db_path: Path | str, *, echo: bool = False) -> Engine:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path.as_posix()}"
    return create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False},  # FastAPI 多线程访问
    )


#: 轻量列迁移表：{表: [(列, DDL 类型, 默认值)]}。
#: 为什么需要：`create_all` 只建**缺的表**，不会给老表加列；没有 alembic 的个人单机
#: 项目里，加列靠"探测 PRAGMA → 缺就 ALTER"，幂等且无需人工介入。
_ADDED_COLUMNS: dict[str, list[tuple[str, str, str]]] = {
    "citation_edges": [("direction", "VARCHAR(16)", "'cites'"), ("year", "INTEGER", "0")],
    # 2026-09-30 账事件与 mecha 对齐：新加两列。⚠ 纯 DDL（ADD COLUMN 带默认值）⇒ 老行
    # **自动**拿到默认值（`kind='history'` 对现有事件是**正确**的：它们全是域事件），
    # **一个 UPDATE 都不用** ⇒ 不碰 events 表那道 append-only 触发器。
    "events": [("kind", "VARCHAR(16)", "'history'"), ("call_id", "VARCHAR(64)", "''")],
}

#: 列**改名**表：{表: [(旧名, 新名)]}。同样纯 DDL：SQLite（≥3.25）`RENAME COLUMN` 会
#: **自动更新触发器/视图里的引用**（本仓 events 上有 append-only 触发器）⇒ 不卸触发器、不回填。
_RENAMED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "events": [("reversible", "undoable")],     # 对齐 mecha 的三态槽位（`None`＝未声明）
}

#: 需要【重建表】才能修的**列可空性**：{表: (列, …)}。
#: 为什么不能只 ALTER：SQLite 不支持改列的可空性 ⇒ 只能"按 ORM 建新表 → 搬数据 → 换名"。
#: 场景（实测踩过）：`undoable` 是从旧列 `reversible`（**NOT NULL**）**改名**来的 ⇒ 约束跟着名字
#: 留在老库里；而 ORM 说这列该可空（mecha 记的是**三态**：`None`＝未声明）⇒ 不修就会在插 `None`
#: 时炸：`gate.seed` 走 mecha 的默认 `undoable=None` ⇒ `IntegrityError: NOT NULL constraint
#: failed: events.undoable`。**全新库**按 ORM 建 ⇒ 天然可空，不会走到这里。
_NULLABLE_REPAIRS: dict[str, tuple[str, ...]] = {
    "events": ("undoable",),
}


def _repair_nullable(conn, table: str, rows) -> None:
    """把 `table` 重建一遍，只让指定列从 NOT NULL 变成可空。

    ⚠ **新表按 ORM 建**（`Event.__table__`）⇒ 列名/类型/默认值/其余约束与 ORM 逐字段一致
    （不手写 DDL —— 手写就有跟 ORM 漂移的风险）。搬数据按**列名交集**显式列列名，不靠列序。
    ⚠ `DROP TABLE` 会连表上的 append-only 触发器一起删，而 `init_db` 后半段用
    `CREATE TRIGGER IF NOT EXISTS` 重建 ⇒ 顺序上安全（本函数在触发器重建之前跑）。
    """
    from .orm import Event  # 延迟导入：不与 orm 的模块级次序耦合

    tmp = f"{table}__rebuilt"
    fresh = Event.__table__.to_metadata(MetaData(), name=tmp)
    fresh.create(conn)
    old_cols = {r[1] for r in rows}
    cols = [c.name for c in fresh.columns if c.name in old_cols]
    collist = ", ".join(f'"{c}"' for c in cols)
    conn.execute(text(f'INSERT INTO "{tmp}" ({collist}) SELECT {collist} FROM "{table}"'))
    conn.execute(text(f'DROP TABLE "{table}"'))
    conn.execute(text(f'ALTER TABLE "{tmp}" RENAME TO "{table}"'))


def _ensure_columns(engine: Engine) -> None:
    """给既有表**补新列 / 改列名**（幂等；老库第一次跑 init_db 时静默补齐）。"""
    with engine.begin() as conn:
        for table, renames in _RENAMED_COLUMNS.items():
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:
                continue
            have = {r[1] for r in rows}
            for old, new in renames:
                if old in have and new not in have:
                    conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"))
        for table, cols in _ADDED_COLUMNS.items():
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:                       # 表还不存在：create_all 已按新 schema 建好
                continue
            have = {r[1] for r in rows}
            for name, ddl, default in cols:
                if name in have:
                    continue
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {name} {ddl} DEFAULT {default}"))
        # ⚠ 放最后：上面的**改名**（`reversible`→`undoable`）正是老库留下 NOT NULL 的来源
        #   ⇒ 必须先改完名，这里才探得到"该可空却仍 NOT NULL"。
        for table, cols in _NULLABLE_REPAIRS.items():
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:
                continue
            if not any(r[1] in cols and r[3] for r in rows):   # r[3] = notnull
                continue
            _repair_nullable(conn, table, rows)


def init_db(engine: Engine) -> None:
    """建表 + FTS5 全文索引表 + events 只增不改触发器（见 orm.Event）+ 轻量列迁移。"""
    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5("
                "title, abstract, tldr, keywords, paper_id UNINDEXED)"
            )
        )
        # append-only 记录仪：数据库层钉死（GAPS.md §3 判据②）——
        # 任何 UPDATE/DELETE events 都被触发器 ABORT，应用层想改也改不了。
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS events_no_update "
                "BEFORE UPDATE ON events "
                "BEGIN SELECT RAISE(ABORT, 'events is append-only: UPDATE forbidden'); END"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS events_no_delete "
                "BEFORE DELETE ON events "
                "BEGIN SELECT RAISE(ABORT, 'events is append-only: DELETE forbidden'); END"
            )
        )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
