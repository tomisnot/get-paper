"""简报渲染契约：领域层只认这个接口，Markdown/HTML 具体格式在 infra 实现。"""

from __future__ import annotations

from typing import Protocol

from ..models import BriefingContent


class BriefRenderer(Protocol):
    def render(self, content: BriefingContent) -> str:
        ...
