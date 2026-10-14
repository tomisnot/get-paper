"""DI 容器：把配置与项目实现绑起来。

内容型 AI 只从 DSH/MCP 的 review 命令进入；本地流水线保留确定性规则和抽取式
摘要作为基线，不再在项目内部维护第二条模型调用通道。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from ..config import Settings, load_settings
from ..domain.pipeline import DailyPipelineService
from ..domain.services import RetrievalService
from ..infra.db import init_db, make_engine, make_session_factory
from ..infra.fts import PaperIndex, PaperTextIndex
from ..infra.notify import NullNotifier, WebhookNotifier
from ..infra.render import MarkdownBriefRenderer
from ..infra.repo import PaperRepository


@dataclass
class Container:
    settings: Settings
    engine: object
    repo: PaperRepository
    pipeline: DailyPipelineService
    retrieval: RetrievalService
    ranker: object | None
    summarizer: object | None
    renderer: MarkdownBriefRenderer
    notifier: object
    ai_provider: str
    ai_note: str = ""
    run_state: dict = field(
        default_factory=lambda: {"running": False, "started_at": None, "last": None}
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or load_settings()
    settings.ensure_dirs()

    engine = make_engine(settings.db_path)
    init_db(engine)
    index = PaperIndex(engine)
    text_index = PaperTextIndex(engine)          # 正文块级（精读体系）：跨篇搜"原文在哪说"
    repo = PaperRepository(make_session_factory(engine), index=index, text_index=text_index)
    # ⭐ **启动期两次写也留痕**（2026-09-26）：从前它们只在框架外悄悄发生——
    # 建表/迁移与"把 YAML 主题真相源对齐进 DB"都是**域状态改动**，却没有任何地方能事后看出
    # "这次启动干了什么"。两条都记**域 journal**（`record_op`：只读面的使用日志，`reversible=0`、
    # **不进 mecha History**——History 只记状态变更），归因 `actor="system"`（不是 human/ai）。
    # ⚠ `init_db` 是**幂等**的（`create_all` + `IF NOT EXISTS`，库内**无版本表**）⇒ 每次启动都会记
    # 一条"启动建表/迁移"，这是**事实陈述**不是噪声；要"只在真变化时记"得先有 schema 版本概念（另批）。
    repo.record_op("migrate", target="schema",
                   after={"ddl": "create_all+fts5+append_only_trigger"},
                   actor="system", reason="启动建表/迁移（幂等）")
    repo.sync_topics(settings.topics)
    repo.record_op("sync_topics_boot", target="topics",
                   after={"count": len(settings.topics)},
                   actor="system", reason="启动把 YAML 主题真相源对齐进 DB")
    # 主题包 → 画像池：**每次启动幂等注入**。旧的"仅空画像才播种一次"会把主题冻成快照
    # （实测：改过的作者永远进不去画像）⇒ 这里每次都对账，主题改动在下次启动/下次主题写入时生效。
    pool = repo.sync_topic_pool(settings.topics, actor="system",
                                reason="启动把主题包幂等注入画像池")
    if pool["injected"] or pool["released"]:
        repo.record_op("sync_topic_pool_boot", target="profile", after=pool,
                       actor="system", reason="启动把主题包幂等注入画像池")

    # 一键流水线只使用确定性基线；外部 AI 评审由 prepare/submit/finalize 命令完成。
    ranker = summarizer = None
    notes: list[str] = []
    resolved = "baseline"

    if settings.notify.enabled and settings.notify.webhook_url:
        notifier: object = WebhookNotifier(settings.notify.webhook_url)
    else:
        notifier = NullNotifier()

    pipeline = DailyPipelineService(
        repo,
        settings=settings,
        ranker=ranker,
        summarizer=summarizer,
        renderer=MarkdownBriefRenderer(),
        notifier=notifier,
        ai_provider=resolved,
    )
    retrieval = RetrievalService(repo)

    return Container(
        settings=settings,
        engine=engine,
        repo=repo,
        pipeline=pipeline,
        retrieval=retrieval,
        ranker=ranker,
        summarizer=summarizer,
        renderer=MarkdownBriefRenderer(),
        notifier=notifier,
        ai_provider=resolved,
        ai_note="；".join(notes),
    )


def run_in_background(
    container: Container, *, stack: dict | None = None, actor: str = "human", reason: str = ""
) -> bool:
    """触发一次每日流水线（后台线程）；已在跑则返回 False。

    actor：Web 手动触发 = "human"，调度器 = "scheduler"（GAPS.md §2 归因）。
    给了 ``stack``（统一启动）则经 mecha 命令面跑（human 通道 + 写权门 + 审计）；
    否则直调 pipeline.run（向后兼容）。
    """
    with container._lock:
        if container.run_state["running"]:
            return False
        container.run_state["running"] = True
        container.run_state["started_at"] = datetime.now()

    def _work() -> None:
        try:
            if stack is not None:
                from ..mecha_adapter.hub import human_write

                res = human_write(stack, "run_pipeline", reason=reason)
                container.run_state["last"] = (
                    res.get("value") if not res.get("is_error") else res.get("error")
                )
            else:
                result = container.pipeline.run(actor=actor, reason=reason)
                container.run_state["last"] = result
        finally:
            container.run_state["running"] = False

    thread = threading.Thread(target=_work, name="paperpilot-run", daemon=True)
    thread.start()
    return True
