"""进程内调度器（APScheduler）：个人单机场景够用。

失效兜底：进程被杀后用 Windows 任务计划 / cron 调 `paperpilot run`（见 README）。
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..domain.pipeline import DailyPipelineService

logger = logging.getLogger("paperpilot.scheduler")


def start_scheduler(pipeline: DailyPipelineService, settings: Settings):
    """启动每日定时跑批；未启用则返回 None。"""
    if not settings.schedule.enabled:
        logger.info("每日调度未启用（settings.schedule.enabled=false）")
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:  # pragma: no cover
        logger.error("apscheduler 未安装，调度未启动: %s", exc)
        return None

    hour_str, _, minute_str = settings.schedule.daily_at.partition(":")
    try:
        trigger = CronTrigger(hour=int(hour_str), minute=int(minute_str or 0))
    except ValueError:
        logger.error("daily_at 格式错误: %r（应为 HH:MM）", settings.schedule.daily_at)
        return None

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        lambda: pipeline.run(actor="scheduler", reason="每日定时跑批"),
        trigger=trigger,
        id="daily-digest",
        replace_existing=True,
        misfire_grace_time=3600,  # 机器睡过点后 1 小时内仍补跑
    )
    scheduler.start()
    logger.info("调度器已启动：每天 %s 自动跑批", settings.schedule.daily_at)
    return scheduler
