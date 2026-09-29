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


class PaperTextIndex:
    """**正文块级**全文索引（精读体系）：跨篇搜"原文里那句话"。

    为什么块级而不是篇级：篇级只能告诉你"这篇里可能有"，块级能告诉你"在**哪一节、哪一块**"
    —— 命中直接带 ``arxiv_id`` + ``block_id``，于是能一步跳到 ``/read/{id}`` 的那一段。
    与 ``papers_fts``（标题/摘要/卡片）**互补**：那个回答"哪篇相关"，这个回答"原文在哪说"。

    派生数据，**不记事件**（与 ``papers_fts`` 同款）；整篇重建保证幂等。
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._ensure()

    def _ensure(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS paper_text_fts USING fts5("
                    "text, arxiv_id UNINDEXED, block_id UNINDEXED, section UNINDEXED, "
                    "kind UNINDEXED, version UNINDEXED)"
                )
            )

    def upsert(self, session: Session, *, arxiv_id: str, version: int, blocks) -> int:
        """整篇重建（幂等）：先删这篇旧行，再逐块插入。``blocks`` 只要求有 text/block_id/
        section/kind 四个属性（鸭子类型，免得 infra 模块互相 import 类型）。"""
        session.execute(
            text("DELETE FROM paper_text_fts WHERE arxiv_id = :a"), {"a": arxiv_id}
        )
        n = 0
        for b in blocks:
            text_value = (getattr(b, "text", "") or "").strip()
            if not text_value:
                continue
            session.execute(
                text(
                    "INSERT INTO paper_text_fts(text, arxiv_id, block_id, section, kind, version) "
                    "VALUES (:text, :a, :b, :s, :k, :v)"
                ),
                {"text": text_value, "a": arxiv_id, "b": getattr(b, "block_id", ""),
                 "s": getattr(b, "section", "") or "", "k": getattr(b, "kind", "") or "",
                 "v": int(version or 0)},
            )
            n += 1
        return n

    def drop(self, session: Session, arxiv_id: str) -> None:
        session.execute(
            text("DELETE FROM paper_text_fts WHERE arxiv_id = :a"), {"a": arxiv_id}
        )

    def search(self, query: str, *, limit: int = 20) -> list[dict]:
        """跨篇块级检索：命中带 ``snippet``（FTS5 自带的高亮片段，用 ``<<>>`` 标出词位）。"""
        match = sanitize_query(query)
        if not match:
            return []
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT arxiv_id, block_id, section, kind, "
                    "snippet(paper_text_fts, 0, '<<', '>>', '…', 18) AS snip "
                    "FROM paper_text_fts WHERE paper_text_fts MATCH :q "
                    "ORDER BY bm25(paper_text_fts) LIMIT :lim"
                ),
                {"q": match, "lim": max(1, int(limit))},
            ).fetchall()
        return [{"arxiv_id": r[0], "block": r[1], "section": r[2], "kind": r[3],
                 "snippet": r[4]} for r in rows]

    def count(self) -> int:
        with self.engine.begin() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM paper_text_fts")).scalar() or 0)
