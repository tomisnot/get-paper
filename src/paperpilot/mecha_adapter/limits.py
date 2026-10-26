"""回程体积闸：不静默丢内容，也不把一整屏塞给模型。

两条纪律（从旧版学来的，实测有效）：

1. **长列表截断必须说出来**：被截的键旁边一定有 `_<键>_truncated`，
   写清 kept / total / where。模型看到"少了"能自己补一步，看到"没有"就永远不知道。
2. **分页续读给明确入口**：能续的（按 offset 的查询）在回执里回 `next_offset`；
   不能续的就指到 Web 面板或缩小参数。

为什么需要它：一次 `read_activity(limit=500)` 或 `query_papers(limit=200)` 能把上下文
直接烧掉一半，而模型真正需要的往往只是"前十行 + 有多长"。
"""

from __future__ import annotations

import json

#: 默认保留条数。20 条足够判断"要不要继续"，又不至于吃掉上下文。
KEEP = 20

#: 会变长的键（只截这些；标量和小对象不动）。
LIST_KEYS = ("papers", "results", "drafts", "events", "references", "citations",
             "related", "clusters", "entries", "nodes", "edges", "notes", "days")

#: 整份回执的字节预算；超了就把列表收到 5 条再报一次。
BUDGET_BYTES = 48 * 1024


def cap(value, *, keep: int = KEEP, where: str = "") -> object:
    """截断长列表并附 `_truncated`。非 dict 原样返回。"""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for key in LIST_KEYS:
        items = out.get(key)
        if isinstance(items, list) and len(items) > keep:
            out[key] = items[:keep]
            out[f"_{key}_truncated"] = {
                "kept": keep,
                "total": len(items),
                "where": where or "全量在 Web 面板；或缩小 limit / 加过滤条件",
            }
    size = len(json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"))
    if size <= BUDGET_BYTES:
        return out
    shrunk = dict(out)
    for key in LIST_KEYS:
        items = shrunk.get(key)
        if isinstance(items, list) and len(items) > 5:
            shrunk[key] = items[:5]
            shrunk[f"_{key}_truncated"] = {
                "kept": 5, "total": len(items),
                "where": "回执超字节预算，只留 5 条；请缩小 days/limit 或改用精确查询",
            }
    shrunk["_bytes"] = {"before": size, "budget": BUDGET_BYTES}
    return shrunk


def with_cap(fn, *, where: str = ""):
    """把一个工具的 execute 包成"结果过体积闸"。"""
    def wrapper(**kwargs):
        return cap(fn(**kwargs), where=where)

    wrapper.__name__ = getattr(fn, "__name__", "tool")
    return wrapper
