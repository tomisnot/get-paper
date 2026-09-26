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


def init_db(engine: Engine) -> None:
    """建表 + FTS5 全文索引表 + events 只增不改触发器（见 orm.Event）。"""
    Base.metadata.create_all(engine)
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
