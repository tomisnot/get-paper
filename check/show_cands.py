"""按切片打印候选的标题+短摘（省上下文，供粗筛）。用法：show_cands.py <file> <start> <end>"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: Path):
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig")
    else:
        text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "{")
    d = json.loads("\n".join(lines[start:]))
    return d.get("candidates") or (d.get("value") or {}).get("candidates") or []


def main() -> None:
    path = Path(sys.argv[1])
    lo, hi = int(sys.argv[2]), int(sys.argv[3])
    width = int(sys.argv[4]) if len(sys.argv) > 4 else 290
    cands = load(path)
    for c in cands[lo:hi]:
        b = c.get("baseline") or {}
        print(f"--- {c.get('arxiv_id')} | {b.get('score', 0):.4f} | {b.get('label')} | "
              f"{c.get('primary_category')}")
        print(f"    T: {c.get('title')}")
        body = str(c.get("abstract") or "").replace(chr(10), " ")[:width]
        print("    A: " + body)


if __name__ == "__main__":
    main()
