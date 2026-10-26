"""数据访问：一处 SQL，一处真相。

约定
- 输入输出都是普通 dict / 基本类型，不暴露 sqlite3.Row 给上层。
- 所有"人的动作"和"AI 的写操作"都要留事件；本模块只做数据，不留事件（事件由调用方记，
  因为只有调用方知道这次动作的语义）。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import db
from .ids import base_id, split_id
from .ledger import EventLog

PAPER_FIELDS = ("arxiv_id", "version", "title", "abstract", "authors", "categories",
                "primary_category", "published_at", "updated_at", "pdf_url", "abs_url")


def _op_id() -> str:
    """当前命令的 operation_id（没有框架栈时为空——单测/纯库用法）。

    为什么在这里取而不是让每个调用点传：**忘了传就是漏账**，而这条关联是
    "框架审计 ↔ 域事实"唯一的对账键。让写口自己取，调用方漏不掉。
    """
    try:
        from mecha.operations import current_operation_id
    except ImportError:                        # 纯库用法（无 mecha 栈）
        return ""
    return current_operation_id()


class Repo:
    def __init__(self, conn):
        self.conn = conn
        self.events_log = EventLog(conn)

    # ------------------------------------------------------------------ 论文
    def upsert_papers(self, papers: Sequence[dict], *, found_by: str = "") -> dict:
        """写入一篇或多篇。已存在的只更新元数据，**首次发现方式不被覆盖**。"""
        now = db.now_iso()
        created, updated = [], []
        for paper in papers:
            aid = base_id(paper.get("arxiv_id", ""))
            if not aid:
                continue
            row = self.conn.execute(
                "SELECT id FROM papers WHERE arxiv_id = ?", (aid,)).fetchone()
            payload = {
                "version": str(paper.get("version") or ""),
                "title": paper.get("title") or "",
                "abstract": paper.get("abstract") or "",
                "authors": db.dumps(list(paper.get("authors") or [])),
                "categories": db.dumps(list(paper.get("categories") or [])),
                "primary_category": paper.get("primary_category") or "",
                "published_at": paper.get("published_at") or None,
                "updated_at": paper.get("updated_at") or None,
                "pdf_url": paper.get("pdf_url") or "",
                "abs_url": paper.get("abs_url") or "",
            }
            if row is None:
                self.conn.execute(
                    "INSERT INTO papers (arxiv_id, version, title, abstract, authors,"
                    " categories, primary_category, published_at, updated_at, pdf_url,"
                    " abs_url, found_by, first_seen_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, payload["version"], payload["title"], payload["abstract"],
                     payload["authors"], payload["categories"], payload["primary_category"],
                     payload["published_at"], payload["updated_at"], payload["pdf_url"],
                     payload["abs_url"], found_by, now))
                created.append(aid)
            else:
                self.conn.execute(
                    "UPDATE papers SET version=?, title=?, abstract=?, authors=?,"
                    " categories=?, primary_category=?, published_at=?, updated_at=?,"
                    " pdf_url=?, abs_url=? WHERE arxiv_id=?",
                    (payload["version"], payload["title"], payload["abstract"],
                     payload["authors"], payload["categories"], payload["primary_category"],
                     payload["published_at"], payload["updated_at"], payload["pdf_url"],
                     payload["abs_url"], aid))
                updated.append(aid)
        self.conn.commit()
        return {"new": len(created), "updated": len(updated),
                "new_ids": created, "updated_ids": updated}

    def _paper_row(self, arxiv_id: str):
        return self.conn.execute(
            "SELECT * FROM papers WHERE arxiv_id = ?", (base_id(arxiv_id),)).fetchone()

    @staticmethod
    def _paper_dict(row) -> dict:
        return {
            "id": row["id"], "arxiv_id": row["arxiv_id"], "version": row["version"],
            "title": row["title"], "abstract": row["abstract"],
            "authors": db.loads(row["authors"], []),
            "categories": db.loads(row["categories"], []),
            "primary_category": row["primary_category"],
            "published_at": row["published_at"], "updated_at": row["updated_at"],
            "pdf_url": row["pdf_url"], "abs_url": row["abs_url"],
            "found_by": row["found_by"], "first_seen_at": row["first_seen_at"],
        }

    def paper(self, arxiv_id: str) -> dict | None:
        row = self._paper_row(arxiv_id)
        return self._paper_dict(row) if row else None

    def paper_view(self, arxiv_id: str) -> dict | None:
        """一篇论文的全部状态：元数据 + 卡 + 人状态 + 计数。详情页与卡片共用。"""
        paper = self.paper(arxiv_id)
        if paper is None:
            return None
        pid = paper["id"]
        card = self.card(pid)
        reading = self.reading(pid)
        paper.update({
            "card": card,
            "reading": reading,
            "has_card": bool(card),
            "notes_count": self.conn.execute(
                "SELECT COUNT(*) c FROM notes WHERE paper_id=?", (pid,)).fetchone()["c"],
            "marks_count": self.conn.execute(
                "SELECT COUNT(*) c FROM marks WHERE paper_id=?", (pid,)).fetchone()["c"],
            "in_drafts": self.drafts_of(paper["arxiv_id"]),
        })
        return paper

    def papers_by_ids(self, arxiv_ids: Iterable[str]) -> dict[str, dict]:
        ids = [base_id(a) for a in arxiv_ids if a]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM papers WHERE arxiv_id IN ({marks})", ids).fetchall()
        return {r["arxiv_id"]: self._paper_dict(r) for r in rows}

    def count_papers(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM papers").fetchone()["c"]

    def list_papers(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM papers ORDER BY published_at DESC, arxiv_id DESC"
            " LIMIT ? OFFSET ?", (int(limit), int(offset))).fetchall()
        return [self._paper_dict(r) for r in rows]

    def search(self, query: str = "", *, limit: int = 50) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return self.list_papers(limit=limit)
        like = f"%{q}%"
        rows = self.conn.execute(
            "SELECT * FROM papers WHERE title LIKE ? OR abstract LIKE ? OR arxiv_id LIKE ?"
            " ORDER BY published_at DESC LIMIT ?", (like, like, like, int(limit))).fetchall()
        return [self._paper_dict(r) for r in rows]

    def recent(self, *, days: float, limit: int = 500) -> list[dict]:
        cutoff = db.days_ago_iso(days)
        rows = self.conn.execute(
            "SELECT * FROM papers WHERE published_at >= ?"
            " ORDER BY published_at DESC LIMIT ?", (cutoff, int(limit))).fetchall()
        return [self._paper_dict(r) for r in rows]

    def between(self, since: str, until: str, *, limit: int = 2000) -> list[dict]:
        """按发布日区间取（`since`/`until` 是 `YYYY-MM-DD` 或完整时标，含两端）。

        `recent(days=…)` 只能表达"最近 N 天"，评不了"某个月"——历史窗口要这个。
        """
        rows = self.conn.execute(
            "SELECT * FROM papers WHERE published_at >= ? AND published_at <= ?"
            " ORDER BY published_at DESC LIMIT ?",
            (f"{since} 00:00:00" if len(since) == 10 else since,
             f"{until} 23:59:59" if len(until) == 10 else until,
             int(limit))).fetchall()
        return [self._paper_dict(r) for r in rows]

    # ------------------------------------------------------------------ 卡
    def card(self, paper_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM cards WHERE paper_id=?", (paper_id,)).fetchone()
        if row is None:
            return None
        return {"tldr": row["tldr"], "problem": row["problem"], "method": row["method"],
                "results": row["results"], "novelty": row["novelty"],
                "terms": db.loads(row["terms"], []), "model": row["model"],
                "written_at": row["written_at"]}

    def write_card(self, arxiv_id: str, card: dict, *, model: str = "") -> bool:
        paper = self.paper(arxiv_id)
        if paper is None:
            return False
        self.conn.execute(
            "INSERT INTO cards (paper_id, tldr, problem, method, results, novelty, terms,"
            " model, written_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(paper_id) DO UPDATE SET tldr=excluded.tldr,"
            " problem=excluded.problem, method=excluded.method, results=excluded.results,"
            " novelty=excluded.novelty, terms=excluded.terms, model=excluded.model,"
            " written_at=excluded.written_at",
            (paper["id"], card.get("tldr", ""), card.get("problem", ""),
             card.get("method", ""), card.get("results", ""), card.get("novelty", ""),
             db.dumps(list(card.get("terms") or [])), model, db.now_iso()))
        self.conn.commit()
        return True

    def cards_of(self, arxiv_ids: Iterable[str]) -> dict[str, dict]:
        ids = [base_id(a) for a in arxiv_ids if a]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT p.arxiv_id, c.* FROM cards c JOIN papers p ON p.id=c.paper_id"
            f" WHERE p.arxiv_id IN ({marks})", ids).fetchall()
        return {r["arxiv_id"]: {
            "tldr": r["tldr"], "problem": r["problem"], "method": r["method"],
            "results": r["results"], "novelty": r["novelty"],
            "terms": db.loads(r["terms"], []), "model": r["model"],
            "written_at": r["written_at"]} for r in rows}

    # ------------------------------------------------------------------ 人的状态
    def reading(self, paper_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM readings WHERE paper_id=?", (paper_id,)).fetchone()
        if row is None:
            return {"read": False, "starred": False, "skipped": False}
        return {"read": bool(row["read"]), "starred": bool(row["starred"]),
                "skipped": bool(row["skipped"])}

    def set_state(self, arxiv_id: str, field: str, value: bool) -> bool | None:
        if field not in ("read", "starred", "skipped"):
            raise ValueError(f"未知状态 {field!r}")
        paper = self.paper(arxiv_id)
        if paper is None:
            return None
        self.conn.execute(
            "INSERT INTO readings (paper_id, read, starred, skipped, updated_at)"
            " VALUES (?,0,0,0,?) ON CONFLICT(paper_id) DO NOTHING",
            (paper["id"], db.now_iso()))
        self.conn.execute(
            f"UPDATE readings SET {field}=?, updated_at=? WHERE paper_id=?",
            (1 if value else 0, db.now_iso(), paper["id"]))
        self.conn.commit()
        return bool(value)

    def starred_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT p.arxiv_id FROM readings r JOIN papers p ON p.id=r.paper_id"
            " WHERE r.starred=1").fetchall()
        return {r["arxiv_id"] for r in rows}

    def star_map(self, arxiv_ids: Iterable[str]) -> dict[str, bool]:
        ids = [base_id(a) for a in arxiv_ids if a]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT p.arxiv_id, r.starred FROM readings r JOIN papers p ON p.id=r.paper_id"
            f" WHERE p.arxiv_id IN ({marks})", ids).fetchall()
        return {r["arxiv_id"]: bool(r["starred"]) for r in rows}

    # ------------------------------------------------------------------ 笔记
    def add_note(self, arxiv_id: str, content: str) -> int | None:
        paper = self.paper(arxiv_id)
        if paper is None:
            return None
        cur = self.conn.execute(
            "INSERT INTO notes (paper_id, content, created_at) VALUES (?,?,?)",
            (paper["id"], content, db.now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def notes(self, arxiv_id: str) -> list[dict]:
        paper = self.paper(arxiv_id)
        if paper is None:
            return []
        rows = self.conn.execute(
            "SELECT * FROM notes WHERE paper_id=? ORDER BY id DESC", (paper["id"],)).fetchall()
        return [{"id": r["id"], "content": r["content"], "created_at": r["created_at"]}
                for r in rows]

    # ------------------------------------------------------------------ 引文边
    def replace_edges(self, src: str, rows: Sequence[dict], *, kind: str = "cites") -> dict:
        """重跑一篇的边：先删后插，幂等。只动这一个 src 的这类边。"""
        src = base_id(src)
        old = self.conn.execute(
            "SELECT COUNT(*) c FROM citation_edges WHERE src=? AND kind=?",
            (src, kind)).fetchone()["c"]
        self.conn.execute("DELETE FROM citation_edges WHERE src=? AND kind=?", (src, kind))
        inserted = 0
        for row in rows:
            dst = base_id(str(row.get("arxiv_id") or ""))
            if not dst or dst == src:
                continue
            self.conn.execute(
                "INSERT INTO citation_edges (src, dst, kind, dst_title, year, citations,"
                " influential) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(src, dst, kind) DO UPDATE SET dst_title=excluded.dst_title,"
                " year=excluded.year, citations=excluded.citations,"
                " influential=excluded.influential",
                (src, dst, kind, str(row.get("title") or "")[:300], int(row.get("year") or 0),
                 int(row.get("citations") or 0), 1 if row.get("influential") else 0))
            inserted += 1
        self.conn.commit()
        return {"src": src, "kind": kind, "removed": old, "added": inserted}

    def merge_incoming(self, dst: str, rows: Sequence[dict], *,
                       kind: str = "cites") -> dict:
        """把"谁引了这篇"并进图，**不删**这些引用者自己的边。

        为什么不能复用 `replace_edges`：它的语义是"这个 src 的边全量重跑"，
        而这里的 src 是别人——删掉会把别人已经同步好的参考文献表一起清空。
        """
        dst = base_id(dst)
        added = 0
        for row in rows:
            src = base_id(str(row.get("arxiv_id") or ""))
            if not src or src == dst:
                continue
            self.conn.execute(
                "INSERT INTO citation_edges (src, dst, kind, dst_title, year, citations,"
                " influential) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(src, dst, kind) DO UPDATE SET dst_title=excluded.dst_title,"
                " year=excluded.year, citations=excluded.citations,"
                " influential=excluded.influential",
                (src, dst, kind, str(row.get("title") or "")[:300], int(row.get("year") or 0),
                 int(row.get("citations") or 0), 1 if row.get("influential") else 0))
            added += 1
        self.conn.commit()
        return {"dst": dst, "kind": kind, "added": added}

    @staticmethod
    def _edge_dict(row, *, direction: str) -> dict:
        return {"arxiv_id": row["other"], "title": row["dst_title"] or "",
                "year": row["year"], "citations": row["citations"],
                "influential": bool(row["influential"]), "direction": direction,
                "in_library": bool(row["in_library"])}

    def edges_from(self, src: str, *, kind: str = "cites") -> list[dict]:
        """它引了谁。"""
        rows = self.conn.execute(
            "SELECT e.dst AS other, e.dst_title, e.year, e.citations, e.influential,"
            " (p.id IS NOT NULL) AS in_library"
            " FROM citation_edges e LEFT JOIN papers p ON p.arxiv_id = e.dst"
            " WHERE e.src=? AND e.kind=? ORDER BY e.citations DESC, e.dst", (base_id(src), kind)
        ).fetchall()
        return [self._edge_dict(r, direction="cites") for r in rows]

    def edges_to(self, dst: str, *, kind: str = "cites") -> list[dict]:
        """谁引了它。"""
        rows = self.conn.execute(
            "SELECT e.src AS other, COALESCE(p.title, e.src) AS dst_title, e.year,"
            " e.citations, e.influential, (p.id IS NOT NULL) AS in_library"
            " FROM citation_edges e LEFT JOIN papers p ON p.arxiv_id = e.src"
            " WHERE e.dst=? AND e.kind=? ORDER BY e.citations DESC, e.src", (base_id(dst), kind)
        ).fetchall()
        return [self._edge_dict(r, direction="cited_by") for r in rows]

    def related(self, arxiv_id: str, *, limit: int = 10) -> list[dict]:
        """共引相似：和它引用集交集最大的库内论文。"""
        aid = base_id(arxiv_id)
        rows = self.conn.execute(
            "SELECT e2.src AS other, COUNT(*) AS shared, p.title AS dst_title,"
            " COALESCE(p.published_at, '') AS year, 0 AS citations, 0 AS influential"
            " FROM citation_edges e1"
            " JOIN citation_edges e2 ON e2.dst = e1.dst AND e2.src != e1.src"
            " JOIN papers p ON p.arxiv_id = e2.src"
            " WHERE e1.src=? AND e1.kind='cites' AND e2.kind='cites'"
            " GROUP BY e2.src ORDER BY shared DESC, e2.src LIMIT ?", (aid, int(limit))
        ).fetchall()
        return [{"arxiv_id": r["other"], "title": r["dst_title"] or "",
                 "shared_references": r["shared"], "in_library": True} for r in rows]

    def upstream_clusters(self, *, min_count: int = 2, limit: int = 20) -> list[dict]:
        """库里多篇反复引的同一文献——领域思想源头。"""
        rows = self.conn.execute(
            "SELECT e.dst AS other, COUNT(DISTINCT e.src) AS n,"
            " MAX(e.dst_title) AS dst_title, MAX(e.year) AS year,"
            " MAX(e.citations) AS citations, MAX(e.influential) AS influential,"
            " (MAX(p.id IS NOT NULL)) AS in_library"
            " FROM citation_edges e LEFT JOIN papers p ON p.arxiv_id = e.dst"
            " WHERE e.kind='cites' GROUP BY e.dst HAVING n >= ?"
            " ORDER BY n DESC, e.dst LIMIT ?", (int(min_count), int(limit))
        ).fetchall()
        return [{"arxiv_id": r["other"], "title": r["dst_title"] or "",
                 "cited_by_in_library": r["n"], "year": r["year"],
                 "citations": r["citations"], "in_library": bool(r["in_library"])}
                for r in rows]

    def edge_counts(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) edges, COUNT(DISTINCT src) sources,"
            " COUNT(DISTINCT dst) targets FROM citation_edges").fetchone()
        return {"edges": row["edges"], "sources": row["sources"], "targets": row["targets"]}

    # ------------------------------------------------------------------ 事件
    def record(self, *, op: str, target: str = "", after: object = None,
               before: object = None, actor: str = "", reason: str = "",
               kind: str = "history", call_id: str = "", operation_id: str = "",
               undoable: bool | None = None, role: str = "action") -> int:
        """记一条账（项目账与框架 History 共用这张表、这个 seq）。

        `operation_id` 由命令面注入（`mecha.operations.current_operation_id()`），
        这是"框架审计"与"域事实"之间唯一的对账键。
        """
        return self.events_log.append({
            "op": op, "target": target, "after": after, "before": before,
            "actor": actor, "reason": reason, "kind": kind, "call_id": call_id,
            "operation_id": operation_id or _op_id(),
            "undoable": undoable, "role": role, "ts": db.now_iso(),
        })

    def events(self, *, limit: int = 100, op_prefix: str = "", actor: str = "",
               target: str = "", since_seq: int = 0) -> list[dict]:
        return self.events_log.recent(limit=limit, op_prefix=op_prefix, actor=actor,
                                      target=target, since_seq=since_seq)

    def signal_events(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, target, op FROM events WHERE op LIKE 'signal:%' ORDER BY seq").fetchall()
        return [{"ts": r["ts"], "target": r["target"], "signal": r["op"].split(":", 1)[1]}
                for r in rows]

    def log_scan(self, *, actor: str, payload: dict) -> int:
        return self.record(op="scan", target="papers", after=payload, actor=actor)

    def scanned_days(self, *, source: str = "") -> set[str]:
        """历史上扫过的自然日（从 scan 事件的 days 明细里取）。"""
        out: set[str] = set()
        rows = self.conn.execute(
            "SELECT after FROM events WHERE op='scan' ORDER BY seq").fetchall()
        for row in rows:
            payload = db.loads(row["after"], {})
            if source and payload.get("source") != source:
                continue
            for day in payload.get("days") or []:
                out.add(str(day)[:10])
        return out

    # ------------------------------------------------------------------ 草稿
    def draft_by_title(self, title: str) -> dict | None:
        """按标题精确找。⚠ 不能用 ``draft()`` 代劳：草稿名常是「1024」这种纯数字，
        而 ``draft()`` 会把纯数字当 id（实测：增量写第二批次时找不到上一批）。"""
        row = self.conn.execute("SELECT * FROM drafts WHERE title=?", (str(title),)).fetchone()
        return self._draft_dict(row) if row else None

    def save_draft(self, *, kind: str, title: str, note: str, payload: dict,
                   author: str = "ai", draft_id: int | None = None) -> int:
        now = db.now_iso()
        if draft_id:
            self.conn.execute(
                "UPDATE drafts SET kind=?, title=?, note=?, payload=?, author=?,"
                " updated_at=? WHERE id=?",
                (kind, title, note, db.dumps(payload), author, now, int(draft_id)))
            self.conn.commit()
            return int(draft_id)
        cur = self.conn.execute(
            "INSERT INTO drafts (kind, title, note, payload, author, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(title) DO UPDATE SET"
            " kind=excluded.kind, note=excluded.note, payload=excluded.payload,"
            " author=excluded.author, updated_at=excluded.updated_at",
            (kind, title, note, db.dumps(payload), author, now, now))
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM drafts WHERE title=?", (title,)).fetchone()
        return int(row["id"]) if row else int(cur.lastrowid or 0)

    def draft(self, ident: str | int | None = None) -> dict | None:
        if ident is None:
            row = self.conn.execute(
                "SELECT * FROM drafts ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
        elif isinstance(ident, int) or str(ident).isdigit():
            # 纯数字既可能是 id 也可能是标题（「1024」这种情景名很常见）：
            # **先按 id 找，找不到再当标题**。设置项里存的是真 id ⇒ 内部调用不受影响；
            # 用户/AI 给的名字 ⇒ 落到标题上（实测：只认 id 时「1024」读不到自己）。
            row = self.conn.execute("SELECT * FROM drafts WHERE id=?", (int(ident),)).fetchone()
            if row is None:
                row = self.conn.execute(
                    "SELECT * FROM drafts WHERE title=?", (str(ident),)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM drafts WHERE title=?", (str(ident),)).fetchone()
        return self._draft_dict(row) if row else None

    @staticmethod
    def _draft_dict(row) -> dict:
        return {"id": row["id"], "kind": row["kind"], "title": row["title"],
                "note": row["note"], "payload": db.loads(row["payload"], {}),
                "author": row["author"], "created_at": row["created_at"],
                "updated_at": row["updated_at"]}

    def drafts(self, *, kind: str = "") -> list[dict]:
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM drafts WHERE kind=?"
                " ORDER BY updated_at DESC, id DESC", (kind,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM drafts ORDER BY updated_at DESC, id DESC").fetchall()
        return [self._draft_dict(r) for r in rows]

    def rename_draft(self, draft_id: int, title: str) -> bool:
        try:
            self.conn.execute("UPDATE drafts SET title=?, updated_at=? WHERE id=?",
                              (title, db.now_iso(), int(draft_id)))
            self.conn.commit()
            return True
        except Exception:  # noqa: BLE001  唯一约束
            return False

    def delete_draft(self, draft_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM drafts WHERE id=?", (int(draft_id),))
        self.conn.commit()
        return cur.rowcount > 0

    def drafts_of(self, arxiv_id: str) -> list[dict]:
        aid = base_id(arxiv_id)
        out = []
        for draft in self.drafts():
            for entry in _entries_of(draft):
                if base_id(entry.get("arxiv_id", "")) == aid:
                    out.append({"id": draft["id"], "title": draft["title"],
                                "kind": draft["kind"], "updated_at": draft["updated_at"]})
                    break
        return out

    def recent_draft_ids(self, *, days: float) -> set[str]:
        cutoff = db.days_ago_iso(days)
        out: set[str] = set()
        for draft in self.drafts():
            if draft["updated_at"] < cutoff:
                continue
            for entry in _entries_of(draft):
                aid = base_id(entry.get("arxiv_id", ""))
                if aid:
                    out.add(aid)
        return out

    # ------------------------------------------------------------------ 设置
    def setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()


def _entries_of(draft: dict) -> list[dict]:
    payload = draft.get("payload") or {}
    if draft.get("kind") == "linear":
        return list(payload.get("entries") or [])
    return [{"arxiv_id": n.get("arxiv_id", "")} for n in payload.get("nodes") or []]


def decayed(weight: float, updated_at: str, half_life_days: float,
            *, now_iso: str | None = None) -> float:
    """读时衰减：w × 2^(−age/half_life)。"""
    ts = db.parse_iso(updated_at)
    now = db.parse_iso(now_iso or db.now_iso())
    if ts is None or now is None or half_life_days <= 0:
        return weight
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return weight * math.pow(2.0, -age_days / float(half_life_days))


def resolve_db_path(settings) -> Path:
    return Path(settings.db_path)
