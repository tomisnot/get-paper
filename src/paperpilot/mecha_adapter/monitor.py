"""PaperPilot 的监控面（Phase 3）：summarizer（人话概括 + Claim 对账）+ cockpit 端点。

两层信任（mecha Monitor 的硬纪律）：
- **概括层**（我们写的、给人看、可能错）：``paperpilot_summarizer`` 把 mecha History
  的事件概括成「写了几次/失败几次/最近命令/当前配置」，其中对配置值的复述用
  ``Claim(value, target=原始键)``——可被原始史证伪。
- **原始层**（权威、append-only）：History 本身。二者不一致时**原始赢**、概括显示
  ``disputed``。

cockpit 是 Monitor 的只读 HTTP 传输（四路由 + CORS + POST 恒 404），框架已实现；
本模块只注入 PaperPilot 的两项：``schema_rows``（配置 schema 行，供 /config 树）与
一条附加路由 ``/summary``（把 MonitorView 投影成 wire 形状）。

与既有 Web ``/activity`` 的分工（Scheme E，两份 journal 互补、非替换）：
- **cockpit**（本模块）= 操作者审计面：mecha History 里的 ``command.<name>`` 审计
  与配置态 KV，喂 dsh 侧边栏 / 面板。
- **Web ``/activity``** = 域数据面：repo.events 的 before→after delta + undo 明细。
"""

from __future__ import annotations

from collections.abc import Mapping

from mecha.cockpit import MonitorEndpoint, monitor_summary_payload
from mecha.history import fold
from mecha.monitor import Claim

from .engine import CONFIG_SCHEMA


#: 配置键 → cockpit /config 树的族（family）。
def _family(key: str) -> str:
    return "scoring" if key.startswith("scoring.") else "fetch"


def config_schema_rows() -> list[dict]:
    """配置 schema 行（cockpit /config 的数据源；单一来源 = engine.CONFIG_SCHEMA）。"""
    return [{"name": name, "family": _family(name), "type": rec.get("type", ""),
             "desc": rec.get("desc", ""), "unit": ""}
            for name, rec in CONFIG_SCHEMA.items()]


def paperpilot_summarizer(events) -> dict:
    """把 mecha History 事件概括成人话读数（含可被原始证伪的 Claim）。

    标量计数（写次数/失败次数/最近命令）是自由描述，不比对；对**配置值**的复述
    用 ``Claim(value, target=原始键)``——Monitor 会拿它与原始 fold 对账，不等则
    整份读数存疑（disputed）。
    """
    snap = fold(events)
    cmds = [e for e in events if e.key.startswith("command.")]
    failed = sum(1 for e in cmds
                 if isinstance(e.value, Mapping) and not e.value.get("ok", True))
    summary: dict = {
        "write_count": len(cmds),
        "failed_writes": failed,
        "last_command": cmds[-1].key[len("command."):] if cmds else "",
        "config_keys_set": sorted(k for k in snap if not k.startswith("command.")),
    }
    # Claim 对账：复述当前生效的配置值（人话键名 → 原始键），只对存在的键下 Claim
    # （对不存在的键下 Claim 会被判 disputed——那是「概括撒谎」的正确表现）。
    for raw_key, label in (("scoring.threshold", "threshold"),
                           ("scoring.max_papers", "max_papers"),
                           ("lookback_days", "lookback_days")):
        if raw_key in snap:
            summary[label] = Claim(snap[raw_key], target=raw_key)
    return summary


def summary_route(stack) -> Mapping:
    """附加路由 /summary 的 handler：把 MonitorView 投影成 wire 形状。"""
    return monitor_summary_payload(stack["monitor"].read())


def make_cockpit(stack, *, host: str = "127.0.0.1", port: int = 0,
                 port_file=None, log=None) -> MonitorEndpoint:
    """造一台 cockpit 只读端点（**未 start**；由调用方 start/stop 握生命周期）。

    source 直接给 ``Software``（cockpit 自动适配 history/gate/authority）；注入
    PaperPilot 的 schema_rows 与 /summary 附加路由。**不传固定端口**（port=0 由
    OS 分配后回读；库内零端口字面量）。
    """
    return MonitorEndpoint(
        stack["software"], host=host, port=port, port_file=port_file,
        schema_rows=config_schema_rows,
        extra_routes={"/summary": lambda _q: summary_route(stack)},
        log=log)


def start_cockpit(stack, *, host: str = "127.0.0.1", port: int = 0,
                  port_file=None, log=None) -> MonitorEndpoint:
    """造并起一台 cockpit 端点，返回已 start 的端点（含 .port/.url）。"""
    endpoint = make_cockpit(stack, host=host, port=port, port_file=port_file, log=log)
    endpoint.start()
    return endpoint
