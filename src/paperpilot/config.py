"""配置加载：YAML 为唯一事实源，支持环境变量覆盖少量键。

设计决策（见 DESIGN.md §9）：
- topics 只存在于 config/settings.yaml，运行期同步进 SQLite topics 表仅用于打分外键，
  避免「数据库一份配置、文件一份配置」漂移。
- data_dir 相对路径基于配置文件所在项目根目录，保证从任何 cwd 启动都写到同一处。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# 包内回退配置（开发时从任意目录启动也能找到项目根）
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class TopicCfg(BaseModel):
    """一个研究主题（打分的最小单位）。"""

    name: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    authors: list[str] = Field(default_factory=list)
    quota: int = 4
    threshold: float = 0.6
    enabled: bool = True


class ScoringCfg(BaseModel):
    threshold: float = 0.6
    quota_per_topic: int = 4
    max_papers: int = 12
    max_per_author: int = 1
    must_read_cap: int = 3


class AICfg(BaseModel):
    """AI 档位。

    两轨（DESIGN.md §17）：
    - **DSH/MCP 轨（主）**：`paperpilot ai` 起 MCP 语义通道，AI 在 DSH 里对话驱动，
      不需要本段任何 key；
    - **程序化轨（辅）**：定时任务无人对话时，unified 档直连 OpenAI 兼容接口
      （DeepSeek 默认）跑打分/精读。key 从 ai.api_key 或环境变量
      PAPERPILOT_API_KEY / DEEPSEEK_API_KEY 读取；没有 key 时 auto 自动落 heuristic。
    """

    provider: Literal["auto", "unified", "heuristic", "off"] = "auto"
    model: str = "deepseek-chat"
    temperature: float = 0.2
    max_concurrency: int = 2
    max_abstract_chars: int = 1600
    # 程序化轨连接参数（OpenAI 兼容；换后端只改这两项）
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 60.0
    max_retries: int = 3


class NotifyCfg(BaseModel):
    enabled: bool = False
    webhook_url: str = ""


class ScheduleCfg(BaseModel):
    enabled: bool = True
    daily_at: str = "07:30"


class WebCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


class MCPCfg(BaseModel):
    """MCP 语义通道（DSH 等 harness 的接入点；仅 localhost）。"""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8780


class Settings(BaseModel):
    data_dir: Path = Path("data")
    lookback_days: int = 7
    arxiv_categories: list[str] = Field(default_factory=lambda: ["cs.CL", "cs.AI", "cs.LG"])
    max_results_per_query: int = 50
    scoring: ScoringCfg = Field(default_factory=ScoringCfg)
    ai: AICfg = Field(default_factory=AICfg)
    notify: NotifyCfg = Field(default_factory=NotifyCfg)
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    web: WebCfg = Field(default_factory=WebCfg)
    mcp: MCPCfg = Field(default_factory=MCPCfg)
    topics: list[TopicCfg] = Field(default_factory=list)
    # 加载来源（不写入 yaml）
    config_path: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "paperpilot.sqlite3"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)


def discover_config() -> Path | None:
    """按优先级找配置文件：--config 显式传入 > 环境变量 > cwd > 包所在项目根。"""
    env = os.environ.get("PAPERPILOT_CONFIG")
    if env:
        return Path(env)
    for candidate in (Path("config/settings.yaml"), _PACKAGE_ROOT / "config/settings.yaml"):
        if candidate.exists():
            return candidate
    return None


def load_settings(path: Path | str | None = None) -> Settings:
    cfg_path = Path(path) if path else discover_config()
    data: dict = {}
    if cfg_path and cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if "PAPERPILOT_DATA_DIR" in os.environ:
        data.setdefault("data_dir", os.environ["PAPERPILOT_DATA_DIR"])

    settings = Settings(**data)
    settings.config_path = cfg_path
    _resolve_data_dir(settings, cfg_path)
    settings.ensure_dirs()
    return settings


def _resolve_data_dir(settings: Settings, cfg_path: Path | None) -> None:
    """相对 data_dir 基于项目根（配置文件所在目录的上一级）。"""
    if not settings.data_dir.is_absolute():
        root = cfg_path.parent.parent if cfg_path else Path.cwd()
        settings.data_dir = (root / settings.data_dir).resolve()


def dump_settings(settings: Settings) -> str:
    """把配置写回 YAML（设置页保存用）。"""
    payload = settings.model_dump(
        exclude={"config_path", "data_dir"}, exclude_none=True
    )
    payload["data_dir"] = str(settings.data_dir)
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def save_settings(settings: Settings, path: Path | str | None = None) -> Path:
    target = Path(path) if path else (settings.config_path or Path("config/settings.yaml"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_settings(settings), encoding="utf-8")
    return target
