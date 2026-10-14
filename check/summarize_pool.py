"""把 prepare_review 的 CLI 回执压成一张精简表（省上下文）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def unwrap(d):
    if isinstance(d, dict) and "ok" in d and "candidates" not in d:
        for key in ("value", "payload", "result", "data"):
            v = d.get(key)
            if isinstance(v, dict):
                return v
    return d


def main() -> None:
    path = Path(sys.argv[1])
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig")
    else:
        text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "{"), None)
    if start is None:
        print("no JSON object found in", path, "| first lines:",
              [ln[:60] for ln in lines[:3]])
        return
    d = json.loads("\n".join(lines[start:]))
    d = unwrap(d)
    cands = d.get("candidates") or []
    print("top-level keys:", list(d.keys())[:14])
    print("ok:", d.get("ok"), "| n_candidates:", d.get("n_candidates"),
          "| shown:", len(cands), "| stage:", d.get("stage"))
    if d.get("interest"):
        it = d["interest"]
        print("interest:", str(it.get("description") or "")[:200])
    for c in cands:
        b = c.get("baseline") or {}
        print(f"{b.get('score', 0):.4f} {b.get('label', ''):<9} {c.get('arxiv_id', ''):>12} "
              f"{str(c.get('title', ''))[:74]}")


if __name__ == "__main__":
    main()
