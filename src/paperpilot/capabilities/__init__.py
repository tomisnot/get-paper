"""连接层：PaperPilot 的"手脚"，protocol-agnostic、自描述、可外部调用。

对外 Python API（docs/SPEC.md §4）：
    from paperpilot.capabilities import build_registry, invoke, specs
    reg = build_registry(container)
    reg.invoke("search_papers", query="rag")   # → 统一信封 dict
    reg.specs()                                 # → 自描述清单（供外部/AI 自省）

便捷函数 invoke(container, name, **params) / specs(container) 会在 container 上缓存 registry。
门面：Python API（本模块）+ CLI（paperpilot tools / call --json）。
未来 MCP/HTTP 只是本层之上的薄 adapter（docs/PRINCIPLES.md 信条 9）。
"""

from __future__ import annotations

from .base import Registry, ToolSpec, err, gate, ok
from .tools import build_registry

__all__ = [
    "Registry", "ToolSpec", "build_registry", "registry_for",
    "invoke", "specs", "err", "gate", "ok",
]


def registry_for(container) -> Registry:
    """复用 container 上缓存的 registry（无则构建并缓存）。"""
    reg = getattr(container, "_capabilities", None)
    if reg is None:
        reg = build_registry(container)
        try:
            container._capabilities = reg
        except Exception:  # noqa: BLE001  # 不可设属性时每次新建，不影响正确性
            pass
    return reg


def invoke(container, name: str, /, **params) -> dict:
    """Python API 门面：调用一个能力，返回统一信封 dict。

    ``container``/``name`` 是 positional-only（``/``），以免与能力自带的
    同名入参（如 add_topic 的 ``name``）撞成重复实参。
    """
    return registry_for(container).invoke(name, **params)


def specs(container) -> list[dict]:
    """Python API 门面：返回全部能力的自描述清单。"""
    return registry_for(container).specs()
