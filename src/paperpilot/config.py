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
    # W4：评审 payload 的基线分下限（低于此不进候选包，降 token；库里仍在，
    # requeue/调低本值可捞回）。只影响评审面信噪比，不影响入库与检索。
    review_floor: float = 0.4


class GraphCfg(BaseModel):
    """图底座呈现参数：**视图未覆盖时**的缺省值（视图 spec 优先，见 GraphView）。

    分工：/settings 的表单改这里（人侧持久化）；AI 侧用 set_graph_view 发布视图
    （落 graph_views 表，即时生效）——两条路互不覆盖，视图优先。
    """

    max_label_len: int = 18
    layer_gap: int = 130
    node_gap: int = 90
    size_by: str = "degree"          # degree|weight|flat
    color_by: str = "auto"           # auto|kind|in_lib|weight|tag|group
    sort_within: str = "weight"      # weight|year（用户：不一定按时间）
    max_nodes: int = 90
    max_edges: int = 400             # 边按权重采样上限（实测 684 条全画=蜘蛛网）
    focus_depth: int = 2             # 单根聚焦缺省深度（/network?root=…&depth= 可覆盖）
    root_default: str = ""           # 无参数打开 /network 时的聚焦根（空＝全库视角）
    layout: str = "layer"            # layer|timeline（年代编排）
    sides: str = "both"              # both|upstream|downstream（有向分层：上游在上/下游在下）
    in_lib_only: bool = False        # 图上只放库内论文（缺的用 materialize_view 入库）
    group_by: str = "none"           # none|tag|group（泳道分组）
    group_quota: int = 0             # 每组保底篇数（0=不保底）
    label_mode: str = "auto"         # auto|always|hover（auto＝布点数少时常显）
    label_style: str = "title"       # title|id
    label_auto_max: int = 90         # auto 模式的"常显"阈值（布点数；默认视图 75 点即可见）
    badge: bool = True               # 标签角标（文字表达，不只靠颜色）
    arrow_size: int = 13             # 箭头像素尺寸（userSpaceOnUse，不随线宽缩）


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


class WebCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


class MCPCfg(BaseModel):
    """MCP 语义通道（mecha 投影；DSH 等 harness 的接入点，仅 localhost）。

    实际端口由 mecha ``McpEndpoint`` 自动选（port=0）并写 ``.mcp-port`` 供发现；
    ``host`` 供绑定地址，``enabled`` 控制 AI 模式是否提醒。``port`` 为兼容保留（当前不钉端口）。
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8780


class Settings(BaseModel):
    data_dir: Path = Path("data")
    lookback_days: int = 7
    arxiv_categories: list[str] = Field(default_factory=lambda: ["cs.CL", "cs.AI", "cs.LG"])
    max_results_per_query: int = 50
    # W6（抓取去噪）：全局兜底查询是否叠加"各主题关键词并集"过滤；
    # 关掉（False）则退回"分类 OR 取最新"的宽进模式（也可靠 topic 查询兼容主题无 keywords）。
    fetch_global_fallback: bool = True
    scoring: ScoringCfg = Field(default_factory=ScoringCfg)
    graph: GraphCfg = Field(default_factory=GraphCfg)
    ai: AICfg = Field(default_factory=AICfg)
    notify: NotifyCfg = Field(default_factory=NotifyCfg)
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

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
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


def discover_example() -> Path | None:
    """找**发布版示例配置** `config/settings.example.yaml`（真配置缺失时的回落）。

    ⚠ 为什么要有这个：`settings.yaml` **不进仓**（开源发布的是示例；见 `.gitignore`）
    ⇒ 陌生人 clone 下来必须**仍有可用配置**，否则"能跑"就成了空话。
    """
    for candidate in (Path("config/settings.example.yaml"),
                      _PACKAGE_ROOT / "config/settings.example.yaml"):
        if candidate.exists():
            return candidate
    return None


def load_settings(path: Path | str | None = None) -> Settings:
    """读配置。**回落链**：显式 `path` > 环境变量 > cwd/项目根 `settings.yaml` > 示例 > 代码内默认值。

    ⚠ 两条不变量：
    * **示例不是真配置**：回落到示例时 `config_path` 记 **None** ⇒ 设置页保存时**不会写回示例**
      （否则 `save_settings` 会改掉发布物），而是按默认目标写我们自己的 `config/settings.yaml`；
    * **显式给的路径不被示例顶替**（显式说去哪儿就去哪儿，哪怕那文件还不存在）。
    """
    explicit = Path(path) if path else None
    cfg_path = explicit or discover_config()
    real_path = cfg_path                     # 真配置（显式给的 / 发现到的）就是写回目标
    if explicit is None and (cfg_path is None or not cfg_path.exists()):
        example = discover_example()
        if example is not None:
            cfg_path = example
        real_path = None                     # 示例不是真配置 ⇒ 不往它里面写回
        # ⚠ 这里**只**在"回落示例"时把 real_path 清成 None：正常发现到真配置时必须保持
        # `cfg_path`（我第一版写成 `real_path = explicit` ⇒ 正常路径也变 None ⇒ 保存会按
        # cwd 相对默认目标写、而非发现到的那个文件；判据
        # `test_discovered_real_config_is_recorded_as_config_path` 钉住这一点）。

    data: dict = {}
    if cfg_path and cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if "PAPERPILOT_DATA_DIR" in os.environ:
        data.setdefault("data_dir", os.environ["PAPERPILOT_DATA_DIR"])

    settings = Settings(**data)
    settings.config_path = real_path
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
