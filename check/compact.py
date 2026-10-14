"""把 JSON 文件压成单行（供 `paperpilot call -p key=<json>` 传参用）。用法：compact.py <in> <out>"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    data = json.loads(src.read_text(encoding="utf-8"))
    dst.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"compacted {src.name} -> {dst.name} ({dst.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
