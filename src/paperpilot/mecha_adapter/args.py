"""参数适配：把 MCP 投影"省略 → None"的调用形状，还原成 handler 的默认值。

**为什么必须有这一层**（实测撞了两次）：

mecha 的工具签名是按 JSON schema **合成的**，不带 Python 默认值。于是"调用方没传某个
可选参数"到达 handler 时是 `None`，而不是 handler 里写的默认值——`int(None)`、
`None.strip()` 立刻炸成 `internal_error`，模型只看到"内部错误"，学不到该怎么做。

`None` 在本项目里不表达语义（"没传"就是"用默认"），所以在这一层补齐。
命令面还额外要吸收框架注入的 `context=`。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable


def _defaults_of(fn: Callable) -> dict:
    return {name: parameter.default
            for name, parameter in inspect.signature(fn).parameters.items()
            if parameter.default is not inspect.Parameter.empty}


def with_defaults(fn: Callable) -> Callable:
    """只读工具用：`None` → 默认值。"""
    defaults = _defaults_of(fn)

    def wrapper(**kwargs):
        for name, value in defaults.items():
            if name not in kwargs or kwargs[name] is None:
                kwargs[name] = value
        return fn(**kwargs)

    wrapper.__name__ = getattr(fn, "__name__", "tool")
    return wrapper


def command_handler(fn: Callable) -> Callable:
    """命令用：吸收 `context=`，并把 `None` 还原成默认值。"""
    defaults = _defaults_of(fn)

    def wrapper(*, context=None, **kwargs):
        for name, value in defaults.items():
            if name not in kwargs or kwargs[name] is None:
                kwargs[name] = value
        return fn(**kwargs)

    wrapper.__name__ = getattr(fn, "__name__", "command")
    return wrapper
