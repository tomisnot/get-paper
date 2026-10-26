"""装配：把配置、数据库、客户端接起来。

一个 Context 就是"这个进程在干活时需要的全部东西"。Web、CLI、工具面共用它。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import db
from .arxiv_client import ArxivClient
from .config import Settings, load_settings
from .repo import Repo
from .scholar import ScholarClient


@dataclass
class Context:
    settings: Settings
    repo: Repo

    def client(self) -> ArxivClient:
        return ArxivClient(min_interval=self.settings.arxiv_min_interval,
                           timeout=self.settings.arxiv_timeout,
                           retries=self.settings.arxiv_retries,
                           page_size=self.settings.page_size)

    def scholar(self) -> ScholarClient:
        return ScholarClient(min_interval=self.settings.scholar_min_interval,
                             timeout=self.settings.arxiv_timeout,
                             retries=self.settings.arxiv_retries)

    @property
    def db_path(self) -> Path:
        return Path(self.settings.db_path)

    def close(self) -> None:
        self.repo.conn.close()


def build_context(settings_path: str | Path | None = None, *,
                  data_dir: str | Path | None = None,
                  settings: Settings | None = None) -> Context:
    settings = settings or load_settings(settings_path)
    if data_dir is not None:
        settings.data_dir = str(data_dir)
    conn = db.open_db(settings.db_path)
    repo = Repo(conn)
    _apply_stored_settings(repo, settings)
    return Context(settings=settings, repo=repo)


def _apply_stored_settings(repo: Repo, settings: Settings) -> None:
    """把 set_config 落到 DB 的覆盖项装回进程。"""
    import json
    raw = repo.setting("settings.overrides", "")
    if not raw:
        return
    try:
        overrides = json.loads(raw)
    except ValueError:
        return
    for key, value in overrides.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
