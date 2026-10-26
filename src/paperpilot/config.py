"""全局配置：dataclass 给默认值，JSON 只写覆盖项。

为什么不是 YAML/TOML：配置项很少且都需要类型，默认值住在代码里最不容易漂；
JSON 是标准库能读的，覆盖文件可以只有三行。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

#: 信号权重：把一个动作折算成多少兴趣增量。可改，改完 recompute 即生效。
#: 数值沿用这人多年的口味标定：下载 > 收藏 > 已读 > 出站 > 浏览。
DEFAULT_SIGNAL_WEIGHTS: dict[str, float] = {
    "view": 0.3,
    "outbound": 0.5,
    "download": 1.0,
    "read": 0.6,
    "star": 0.8,
    "skip": -1.0,
    "uninterested": -1.5,
}


@dataclass
class Settings:
    # 路径
    data_dir: str = "data"
    db_name: str = "paperpilot.sqlite3"

    # arXiv 客户端
    arxiv_min_interval: float = 3.0
    arxiv_timeout: float = 30.0
    arxiv_retries: int = 3
    page_size: int = 50
    max_pages_per_day: int = 4
    max_requests: int = 120

    # Semantic Scholar（引文唯一来源；无 key 时限速约 1 次/秒）
    scholar_min_interval: float = 1.1

    # 兴趣
    half_life_days: float = 30.0
    signal_weights: dict = field(default_factory=lambda: dict(DEFAULT_SIGNAL_WEIGHTS))

    # 排序与抽样
    default_regions_n: int = 5
    score_floor: float = 0.10

    # 草稿体检
    recent_draft_days: int = 7

    # 服务（8080 被另装的旧实例占着，这一版默认换端口，避免撞车）
    host: str = "127.0.0.1"
    port: int = 8090

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / self.db_name

    def to_dict(self) -> dict:
        return asdict(self)


def load_settings(path: str | Path | None = None) -> Settings:
    """读 settings.json（若有），只覆盖认识的键。"""
    settings = Settings()
    if path is None:
        return settings
    path = Path(path)
    if not path.exists():
        return settings
    raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    known = {f.name for f in fields(Settings)}
    for key, value in raw.items():
        if key in known:
            setattr(settings, key, value)
    return settings


def dump_settings(settings: Settings) -> str:
    return json.dumps(settings.to_dict(), ensure_ascii=False, indent=2)
