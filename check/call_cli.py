"""用 subprocess(argv 列表) 调 paperpilot CLI，避免 PowerShell 引号转义问题。

用法：call_cli.py <tool> [key=value | key=@jsonfile] ...
      -- 参数值以 @ 开头时，从该文件读取（先压成单行）。
输出：exit 码 + 关键字段摘要；原始 stdout 落 check/last_call.out。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

EXE = Path(".venv/Scripts/paperpilot.exe")
RAW_OUT = Path("check/last_call.out")
KEYS = ("ok", "accepted", "rejected", "selected", "briefing_id", "n_candidates",
        "n_after_rules", "reused", "run_id", "date", "error", "count")


def build_args(tool: str, pairs: list[str]) -> list[str]:
    args = [str(EXE), "call", tool]
    for pair in pairs:
        key, _, value = pair.partition("=")
        if value.startswith("@"):
            data = json.loads(Path(value[1:]).read_text(encoding="utf-8"))
            value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        args += ["-p", f"{key}={value}"]
    return args


def main() -> None:
    tool, pairs = sys.argv[1], sys.argv[2:]
    proc = subprocess.run(build_args(tool, pairs), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "").strip()
    RAW_OUT.write_text(out or "(empty)", encoding="utf-8")
    print("exit:", proc.returncode)
    err = (proc.stderr or "").strip().splitlines()
    if err:
        print("stderr:", err[0][:200])
    lines = out.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "{"), None)
    if start is None:
        print("no json in stdout; raw head:", lines[:3])
        return
    try:
        data = json.loads("\n".join(lines[start:]))
    except json.JSONDecodeError as exc:
        print("json decode failed:", exc)
        return
    brief = {k: data[k] for k in KEYS if k in data}
    print("summary:", json.dumps(brief, ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()
