"""只读查询：注册到 Surface.QUERY，供宿主/面板/监控调用，不经写权门。

查询必须无副作用——包括不能顺手记一条 `signal:view`。
"""

from __future__ import annotations

from mecha.surface import EntryKind

from .. import scoring
from ..context import Context
from . import config as config_mod


def build_queries(ctx: Context, software) -> list[str]:
    """注册查询，返回已注册的名字（供测试与能力图对齐）。"""
    repo, settings = ctx.repo, ctx.settings

    def describe_state() -> dict:
        counts = repo.count_papers()
        cards = repo.conn.execute("SELECT COUNT(*) c FROM cards").fetchone()["c"]
        drafts = repo.conn.execute("SELECT COUNT(*) c FROM drafts").fetchone()["c"]
        events = repo.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        return {"papers": counts, "cards": cards, "starred": len(repo.starred_ids()),
                "drafts": drafts, "events": events,
                "scanned_days": len(repo.scanned_days()),
                "edges": repo.edge_counts()}

    def read_settings() -> dict:
        return {"config": {key: getattr(settings, key) for key in config_mod.CONFIG_KEYS},
                "half_life_days": settings.half_life_days,
                "signal_weights": dict(settings.signal_weights)}

    def read_profile(top: int = 12) -> dict:
        return scoring.profile_view(repo, settings, top=int(top))

    registered = []
    for name, fn in (("describe_state", describe_state),
                     ("read_settings", read_settings),
                     ("read_profile", read_profile)):
        software.surface.register(EntryKind.QUERY, name, fn, called_by="framework")
        registered.append(name)
    return registered
