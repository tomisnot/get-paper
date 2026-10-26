"""命令行入口：所有子命令都经 mecha 装配后的**同一个栈**。

    python -m paperpilot init
    python -m paperpilot tools
    python -m paperpilot call read_status -p '{}'
    python -m paperpilot serve
"""

from __future__ import annotations

import sys

from .mecha_adapter.hub import main

__all__ = ["main"]

if __name__ == "__main__":
    sys.exit(main())
