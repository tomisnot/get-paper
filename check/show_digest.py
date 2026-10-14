"""从 call_cli.py 落盘的原始 stdout 里打印简报统计与条目。用法：show_digest.py <rawfile>"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    lines = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "{")
    d = json.loads("\n".join(lines[start:]))
    print("stats:", json.dumps(d.get("stats") or {}, ensure_ascii=False))
    items = d.get("items") or []
    print("items:", len(items))
    for i, it in enumerate(items, 1):
        print(f"{i:>2}. {it.get('score', 0):.3f} {it.get('label', ''):<9} "
              f"{it.get('arxiv_id', ''):>12} {str(it.get('title', ''))[:62]}")
        print(f"      why: {str(it.get('reason') or '')[:110]}")
        s = it.get("summary") or {}
        if s.get("tldr"):
            print(f"      tldr: {str(s['tldr'])[:90]}")


if __name__ == "__main__":
    main()
