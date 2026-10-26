"""监控面：只读摘要（可被原始史证伪的 Claim）+ cockpit 端点。

两层信任：`summarize` 是**派生层**（可能错），History 是**原始层**（权威）。
对配置值的复述一律写成 `Claim(value, target=键)`，框架会拿它跟原始折叠对账，
不等就把整份读数标成存疑——这让"概括撒谎"在机制上暴露，而不是靠人自觉。
"""

from __future__ import annotations

from collections.abc import Mapping

from mecha.cockpit import MonitorEndpoint, monitor_summary_payload
from mecha.history import fold
from mecha.monitor import Claim

from . import config as config_mod


def summarize(events) -> Mapping:
    """把事件史概括成人话读数。计数是自由描述，配置值必须下 Claim。"""
    snap = fold(events)
    commands = [e for e in events if e.op.startswith("command.")]
    failed = sum(1 for e in commands
                 if isinstance(e.after, Mapping) and not e.after.get("ok", True))
    summary: dict = {
        "writes": len(commands),
        "failed_writes": failed,
        "last_command": commands[-1].op[len("command."):] if commands else "",
        "config_keys_set": sorted(snap),
    }
    for key in ("half_life_days", "default_regions_n", "max_requests"):
        if key in snap:
            summary[key] = Claim(snap[key], target=key)
    return summary


def make_cockpit(stack, *, host: str = "127.0.0.1", port: int = 0,
                 port_file=None, log=None) -> MonitorEndpoint:
    return MonitorEndpoint(
        stack.software, host=host, port=port, port_file=port_file,
        schema_rows=config_mod.schema_rows,
        extra_routes={"/summary": lambda _q: monitor_summary_payload(
            stack.software.monitor.read())},
        log=log)


def start_cockpit(stack, *, host: str = "127.0.0.1", port: int = 0,
                  port_file=None, log=None) -> MonitorEndpoint:
    endpoint = make_cockpit(stack, host=host, port=port, port_file=port_file, log=log)
    endpoint.start()
    return endpoint
