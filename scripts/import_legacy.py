"""一次性导入：把一个既有 SQLite 里的**数据**搬进新库。

搬什么（docs/设计-总纲.md 附录 A）：论文、卡、人的状态、笔记、信号账本、引文边。
不搬什么：任何页面产出物（草稿/简报/期票那一类），以及权重表本身
（权重由信号重算，重算结果和来源做排序比对）。

用法：
    python scripts/import_legacy.py --source "D:\\path\\old.sqlite3" --data-dir data
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paperpilot import db, scoring  # noqa: E402
from paperpilot.config import Settings  # noqa: E402
from paperpilot.context import build_context  # noqa: E402


def open_old(path: str) -> sqlite3.Connection:
    """只读打开，遇磁盘抖动重试（这类库常在同步盘里）。"""
    last = None
    for _ in range(5):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT COUNT(*) FROM papers").fetchone()
            return conn
        except sqlite3.Error as exc:  # noqa: PERF203
            last = exc
            time.sleep(1.0)
    raise SystemExit(f"打不开源库：{last}")


def load_cards(old: sqlite3.Connection) -> dict[int, dict]:
    """每篇取最新的一份总结（按 id 最大）。"""
    rows = old.execute(
        "SELECT s.* FROM paper_summaries s JOIN ("
        "  SELECT paper_id, MAX(id) AS mid FROM paper_summaries GROUP BY paper_id"
        ") t ON t.mid = s.id").fetchall()
    out = {}
    for row in rows:
        terms = []
        try:
            raw = json.loads(row["terms"] or "[]")
        except ValueError:
            raw = []
        for item in raw:
            if isinstance(item, dict):
                term = item.get("canonical") or item.get("term") or ""
            else:
                term = str(item)
            if term:
                terms.append(term)
        out[row["paper_id"]] = {"tldr": row["tldr"] or "", "problem": row["problem"] or "",
                                "method": row["method"] or "", "results": row["results"] or "",
                                "novelty": row["novelty"] or "", "terms": terms,
                                "model": row["model"] or ""}
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    old = open_old(args.source)
    settings = Settings(data_dir=args.data_dir)
    ctx = build_context(settings=settings)
    repo = ctx.repo
    now = db.now_iso()

    papers = old.execute("SELECT * FROM papers").fetchall()
    print(f"源库论文 {len(papers)} 篇")

    new_ids: dict[int, int] = {}
    batch = []
    for row in papers:
        batch.append({
            "arxiv_id": row["arxiv_id"], "version": row["version"] or "",
            "title": row["title"] or "", "abstract": row["abstract"] or "",
            "authors": json.loads(row["authors"] or "[]"),
            "categories": json.loads(row["categories"] or "[]"),
            "primary_category": row["primary_category"] or "",
            "published_at": row["published_at"], "updated_at": row["updated_at"],
            "pdf_url": row["pdf_url"] or "", "abs_url": row["abs_url"] or "",
        })
    result = repo.upsert_papers(batch, found_by="legacy")
    print(f"论文落库：新 {result['new']} / 更新 {result['updated']}")

    for row in old.execute("SELECT id, arxiv_id FROM papers").fetchall():
        target = repo.conn.execute("SELECT id FROM papers WHERE arxiv_id=?",
                                   (row["arxiv_id"],)).fetchone()
        if target:
            new_ids[row["id"]] = target["id"]

    cards = load_cards(old)
    inserted = 0
    with_cards = []
    for old_pid, card in cards.items():
        new_pid = new_ids.get(old_pid)
        if new_pid is None:
            continue
        with_cards.append((new_pid, card["tldr"], card["problem"], card["method"],
                           card["results"], card["novelty"],
                           json.dumps(card["terms"], ensure_ascii=False),
                           card["model"], now))
    if with_cards and not args.dry_run:
        repo.conn.executemany(
            "INSERT INTO cards (paper_id, tldr, problem, method, results, novelty, terms,"
            " model, written_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(paper_id) DO UPDATE SET tldr=excluded.tldr,"
            " problem=excluded.problem, method=excluded.method, results=excluded.results,"
            " novelty=excluded.novelty, terms=excluded.terms, model=excluded.model,"
            " written_at=excluded.written_at", with_cards)
        repo.conn.commit()
    inserted = len(with_cards)
    print(f"卡：{inserted} 份")

    readings = []
    for row in old.execute("SELECT * FROM reading_state").fetchall():
        new_pid = new_ids.get(row["paper_id"])
        if new_pid is None:
            continue
        readings.append((new_pid, int(row["read"] or 0), int(row["star"] or 0),
                         int(row["marked_skip"] or 0), row["updated_at"] or now))
    if readings and not args.dry_run:
        repo.conn.executemany(
            "INSERT INTO readings (paper_id, read, starred, skipped, updated_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET"
            " read=excluded.read, starred=excluded.starred, skipped=excluded.skipped",
            readings)
        repo.conn.commit()
    print(f"人的状态：{len(readings)} 条")

    notes = []
    for row in old.execute("SELECT * FROM notes").fetchall():
        new_pid = new_ids.get(row["paper_id"])
        if new_pid is None:
            continue
        notes.append((new_pid, row["content"] or "", row["created_at"] or now))
    if notes and not args.dry_run:
        repo.conn.executemany(
            "INSERT INTO notes (paper_id, content, created_at) VALUES (?,?,?)", notes)
        repo.conn.commit()
    print(f"笔记：{len(notes)} 条")

    signals = []
    for row in old.execute(
            "SELECT ts, op, target, after FROM events WHERE op LIKE 'signal:%' ORDER BY seq").fetchall():
        aid = row["target"]
        if not aid:
            try:
                aid = json.loads(row["after"] or "{}").get("arxiv_id", "")
            except ValueError:
                aid = ""
        if not aid:
            continue
        signals.append((row["ts"], "human", row["op"], aid,
                        row["after"] if isinstance(row["after"], str) else "{}"))
    if signals and not args.dry_run:
        inserted_before = repo.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE op LIKE 'signal:%'").fetchone()["c"]
        repo.conn.executemany(
            "INSERT INTO events (ts, kind, op, target, after, actor, reason)"
            " SELECT ?,'history',?,?,?,'human','legacy 导入' WHERE NOT EXISTS ("
            "  SELECT 1 FROM events WHERE ts=? AND op=? AND target=?)",
            [(ts, op, target, payload, ts, op, target)
             for ts, op, target, payload in signals])
        repo.conn.commit()
        inserted_after = repo.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE op LIKE 'signal:%'").fetchone()["c"]
        print(f"信号：源库 {len(signals)} 条，本次新增 {inserted_after - inserted_before} 条")

    edges = []
    for row in old.execute("SELECT * FROM citation_edges").fetchall():
        edges.append((row["src_arxiv_id"], row["dst_arxiv_id"],
                      row["direction"] or "cites", row["dst_title"] or "",
                      int(row["year"] or 0), int(row["dst_citations"] or 0),
                      int(row["influential"] or 0)))
    if edges and not args.dry_run:
        repo.conn.executemany(
            "INSERT INTO citation_edges (src, dst, kind, dst_title, year, citations, influential)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(src, dst, kind) DO NOTHING", edges)
        repo.conn.commit()
    print(f"引文边：{len(edges)} 条")

    if args.dry_run:
        print("dry-run：不重算权重")
        return 0

    stats = scoring.recompute_profile(repo, settings.signal_weights)
    print(f"权重重算：信号 {stats['signals']} 条 → 键 {stats['keys']} 个；"
          f"找不到的论文 {len(stats['missing_papers'])} 篇")

    compare(old, repo)
    ctx.close()
    return 0


def compare(old: sqlite3.Connection, repo) -> None:
    """只比排序，不比数值（数值受衰减时点与口径影响）。"""
    print("\n与源库画像的排序比对（top-10 重合度）：")
    snapshot = scoring.profile_snapshot(repo, 30.0)
    for kind in ("category", "term", "author"):
        legacy = [row["key"] for row in old.execute(
            "SELECT key, w FROM profile_weights WHERE kind=? AND w > 0"
            " ORDER BY w DESC LIMIT 10", (kind,)).fetchall()]
        mine = [key for key, value in sorted(
            ((k, v["w_raw"]) for k, v in snapshot[kind].items() if v["w_raw"] > 0),
            key=lambda kv: -kv[1])[:10]]
        overlap = len(set(legacy) & set(mine))
        print(f"  {kind:9s} 源库 {len(legacy)} 项 / 新库 {len(mine)} 项 / 重合 {overlap}")
        print(f"    源库 top: {', '.join(legacy[:6])}")
        print(f"    新库 top: {', '.join(mine[:6])}")


if __name__ == "__main__":
    raise SystemExit(main())
