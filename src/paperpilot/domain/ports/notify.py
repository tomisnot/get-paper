"""通知契约：飞书/钉钉/邮件等一律实现这个 Port（默认关闭）。"""

from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    def notify(self, *, title: str, markdown: str, date: str) -> bool:
        """推送一条简报；返回是否发送成功。失败不得影响主流程。"""
        ...
