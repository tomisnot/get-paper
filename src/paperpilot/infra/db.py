"""数据库引擎与会话工厂（SQLite，单机个人使用）。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
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
}


def _ensure_columns(engine: Engine) -> None:
    """给既有表补新列（幂等；老库第一次跑 init_db 时静默补齐）。"""
    with engine.begin() as conn:
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
