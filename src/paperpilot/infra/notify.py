"""通知：飞书/钉钉/企业微信 Webhook（默认关闭）。失败绝不抛给主流程。"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("paperpilot.notify")

_MAX_PUSH_CHARS = 3500


class NullNotifier:
    """默认实现：什么都不做。"""

    name = "null"

    def notify(self, *, title: str, markdown: str, date: str) -> bool:
        return False


class WebhookNotifier:
    """飞书风格 text 消息（钉钉/企微改 payload 即可，接口不变）。"""

    name = "webhook"

    def __init__(self, url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.url = url
        self._transport = transport

    def notify(self, *, title: str, markdown: str, date: str) -> bool:
        text = f"📄 {title}\n\n{_strip_markdown(markdown)}"
        payload = {"msg_type": "text", "content": {"text": text[:_MAX_PUSH_CHARS]}}
        try:
            with httpx.Client(timeout=15, transport=self._transport) as client:
                resp = client.post(self.url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Webhook 通知失败: %s", exc)
            return False


def _strip_markdown(text: str) -> str:
    """推送通道只收纯文本：去掉 Markdown 记号。"""
    out_lines: list[str] = []
    for line in text.splitlines():
        line = line.lstrip("#").strip()
        line = line.replace("**", "").replace("`", "")
        if line.startswith("> ⚠️"):
            line = "⚠️" + line[4:]
        out_lines.append(line)
    return "\n".join(out_lines)
