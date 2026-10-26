"""可写配置：键、类型、值域、校验、种入 Gate。

单一来源：这张表既是 Gate 的校验规则，也是监控面 `/config` 树的 schema 行。
"""

from __future__ import annotations

from dataclasses import dataclass

from mecha.errors import MechaError, UnknownKey

from ..config import Settings


@dataclass(frozen=True)
class Key:
    kind: str          # "int" | "float"
    lo: float
    hi: float
    desc: str


CONFIG_SCHEMA: dict[str, Key] = {
    "half_life_days": Key("float", 1.0, 365.0, "兴趣权重的半衰期（天）"),
    "default_regions_n": Key("int", 1, 12, "抓取时按画像取几个分类"),
    "page_size": Key("int", 10, 200, "arXiv 单页条数"),
    "max_pages_per_day": Key("int", 1, 10, "每个自然日最多翻几页"),
    "max_requests": Key("int", 5, 400, "单次抓取的请求硬上限"),
    "arxiv_min_interval": Key("float", 0.0, 30.0, "两次 arXiv 请求的最小间隔（秒）"),
    "scholar_min_interval": Key("float", 0.0, 10.0, "两次 Semantic Scholar 请求的最小间隔（秒）"),
    "recent_draft_days": Key("float", 1.0, 90.0, "体检回看多少天内的草稿"),
    "score_floor": Key("float", 0.0, 1.0, "低于这个画像分的不进候选"),
}

CONFIG_KEYS = tuple(CONFIG_SCHEMA)


def validate(key: str, value: object) -> None:
    """Gate 的校验器：键要在表里，值要能转成声明的类型并落在值域内。"""
    spec = CONFIG_SCHEMA.get(key)
    if spec is None:
        raise UnknownKey.typo(key, CONFIG_KEYS, kind="unknown_key",
                              hint="只有 CONFIG_SCHEMA 里的键可写")
    try:
        cast = float(value) if spec.kind == "float" else int(value)
    except (TypeError, ValueError) as exc:
        raise MechaError(
            f"配置 {key!r} 需要一个 {spec.kind}，收到 {value!r}",
            kind="bad_config_value",
            hint=f"取值范围 {spec.lo} ~ {spec.hi}",
        ) from exc
    if not spec.lo <= float(cast) <= spec.hi:
        raise MechaError(
            f"配置 {key!r} 越界（{spec.lo} ~ {spec.hi}），收到 {cast}",
            kind="bad_config_value",
            hint="先 read_config 看当前值，再改一小步",
        )


def coerce(key: str, value: object) -> object:
    spec = CONFIG_SCHEMA[key]
    return float(value) if spec.kind == "float" else int(value)


def seed(gate, settings: Settings) -> None:
    """把 settings 里的当前值种进 Gate（seed 不过写权门，actor=bootstrap）。

    ⚠ **只在值真的变了才写**：`Gate.seed` 每调一次追加一条事件，而软件每次启动都要
    seed 一遍 ⇒ 不改这一条，事件流会被「9 个配置键 × 每次重启」淹没，AI 调
    `read_activity` 看到的全是 `set page_size 50`，真正的事件反而被埋在下面。
    与当前快照相同的键直接跳过（幂等）。
    """
    snapshot = gate.snapshot
    for key in CONFIG_KEYS:
        value = coerce(key, getattr(settings, key))
        if snapshot.get(key) == value:
            continue
        gate.seed(key, value, actor="bootstrap", reason="seed from settings")


def apply_snapshot(settings: Settings, snapshot: dict) -> Settings:
    """把 Gate 快照装回进程内 settings（改完立刻生效）。"""
    for key, value in (snapshot or {}).items():
        if key in CONFIG_SCHEMA and value is not None:
            setattr(settings, key, coerce(key, value))
    return settings


def schema_rows() -> list[dict]:
    """监控面 /config 树的数据源。"""
    return [{"name": name, "family": "兴趣" if name == "half_life_days" else "抓取",
             "type": spec.kind, "desc": spec.desc, "unit": ""}
            for name, spec in CONFIG_SCHEMA.items()]
