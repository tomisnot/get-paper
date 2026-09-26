"""FTS5 全文检索索引（文章表 + 总结 tldr/keywords 一起可搜）。

选用普通 FTS5 表（而非 external content）的原因：
- 摘要入库/更新时手动维护一行，逻辑直白，不受触发器与 ORM 交互顺序影响
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

_TOKEN_RE = re.compile(r"[\w\-\.]+")


def sanitize_query(query: str) -> str | None:
    """把用户输入转成安全的 FTS5 MATCH 短语；空输入返回 None。"""
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


class PaperIndex:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._ensure()

    def _ensure(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5("
                    "title, abstract, tldr, keywords, paper_id UNINDEXED)"
                )
            )

    def upsert(
        self,
        session: Session,
        *,
        paper_id: int,
        title: str,
        abstract: str,
        tldr: str = "",
        keywords: Sequence[str] = (),
    ) -> None:
        session.execute(
            text("DELETE FROM papers_fts WHERE paper_id = :pid"), {"pid": paper_id}
        )
        session.execute(
            text(
                "INSERT INTO papers_fts(title, abstract, tldr, keywords, paper_id) "
                "VALUES (:title, :abstract, :tldr, :keywords, :pid)"
            ),
            {
                "title": title or "",
                "abstract": abstract or "",
                "tldr": tldr or "",
                "keywords": " ".join(keywords or []),
                "pid": paper_id,
            },
        )

    def delete(self, session: Session, paper_id: int) -> None:
        session.execute(text("DELETE FROM papers_fts WHERE paper_id = :pid"), {"pid": paper_id})

    def search(self, query: str, *, limit: int = 50) -> list[int]:
        """返回按相关度排序的 paper 主键列表。"""
        match = sanitize_query(query)
        if not match:
            return []
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT paper_id FROM papers_fts "
                    "WHERE papers_fts MATCH :q ORDER BY bm25(papers_fts) LIMIT :lim"
                ),
                {"q": match, "lim": limit},
            )
            return [int(r[0]) for r in rows]
