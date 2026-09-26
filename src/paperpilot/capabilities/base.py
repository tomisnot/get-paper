"""中性能力层框架（docs/SPEC.md §4 / docs/PRINCIPLES.md 信条 9）。

把"手脚"整理成 **protocol-agnostic、自描述、可外部调用** 的工具，与任何具体 AI
接入协议解耦。三条纪律沿用仓库既有实践，但**不绑定 MCP**：

1. **自描述**：每个能力带 name/description/入参 schema/read|write/reversible，
   外部（含未来的 AI harness）可 `specs()` 自省后再调用；
2. **统一返回信封**：`{ok:True, ...}` 或 `{ok:False, error:{kind,message,hint,suggest}}`；
3. **体积闸 + 可教学错误**：大回程截断并附「截了多少/去哪看全量」；错误带下一步提示。

门面：Python API（`invoke`/`specs`）+ CLI（`paperpilot tools` / `call --json`）。
未来 MCP/HTTP 只是本层之上的薄 adapter。
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..infra.ai.errors import AIError
from ..infra.arxiv import ArxivError
from ..infra.scholar import ScholarError

logger = logging.getLogger("paperpilot.capabilities")

# 回程预算（字节）与长列表保留条数（与旧 mcp_server 同源纪律）
_RETURN_BUDGET = 65536
_LIST_KEEP = 20
_LIST_KEYS = (
    "items", "papers", "candidates", "archived", "briefings",
    "topics", "events", "results", "scores",
)

_PY2JSON = {str: "string", int: "integer", float: "number", bool: "boolean",
            list: "array", dict: "object"}

# from __future__ import annotations 会把注解变成字符串（如 "int"）——两种都要认
_STR2JSON = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
             "list": "array", "dict": "object"}


def _json_type(hint: Any) -> str:
    if isinstance(hint, str):
        return _STR2JSON.get(hint.split("[", 1)[0].strip(), "string")
    origin = getattr(hint, "__origin__", None)
    if origin is list:
        return "array"
    if origin is dict:
        return "object"
    return _PY2JSON.get(hint, "string")


@dataclass
class ToolSpec:
    """一个能力的自描述元数据 + 处理函数。"""

    name: str
    description: str
    handler: Callable[..., dict]
    kind: Literal["read", "write"] = "read"
    reversible: bool = False
    params: dict[str, dict] = field(default_factory=dict)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "reversible": self.reversible,
            "params": self.params,
        }


def params_from_signature(fn: Callable) -> dict[str, dict]:
    """从函数签名 + 类型注解推导入参 schema（自描述用）。"""
    hints = dict(getattr(fn, "__annotations__", {}))
    out: dict[str, dict] = {}
    for name, p in inspect.signature(fn).parameters.items():
        if name in ("self", "cls"):
            continue
        info: dict[str, Any] = {"type": _json_type(hints.get(name, str))}
        if p.default is inspect.Parameter.empty:
            info["required"] = True
        else:
            info["required"] = False
            info["default"] = p.default
        out[name] = info
    return out


def ok(**data: Any) -> dict:
    """成功信封。"""
    return {"ok": True, **data}


def err(kind: str, message: str, *, hint: str = "", suggest: list | None = None) -> dict:
    """可教学错误信封。"""
    error: dict[str, Any] = {"kind": kind, "message": message}
    if hint:
        error["hint"] = hint
    if suggest:
        error["suggest"] = suggest
    return {"ok": False, "error": error}


def gate(obj: Any, budget: int = _RETURN_BUDGET) -> Any:
    """体积闸：截断长列表并附 kept/total/where；整体超预算则回预览（不静默）。"""
    if not isinstance(obj, dict):
        return obj
    for key in _LIST_KEYS:
        value = obj.get(key)
        if isinstance(value, list) and len(value) > _LIST_KEEP:
            obj[key] = value[:_LIST_KEEP]
            obj[f"_{key}_truncated"] = {
                "kept": _LIST_KEEP,
                "total": len(value),
                "where": "全量见 Web 面板 /papers，或按 id 精确查询（get_paper）",
            }
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return obj
    if len(text.encode("utf-8")) <= budget:
        return obj
    return {
        "ok": obj.get("ok", True),
        "truncated": True,
        "kept_bytes": budget,
        "total_bytes": len(text.encode("utf-8")),
        "where": "回程超体积闸：缩小 limit/days，或改看 Web 面板",
        "preview": text[:2000],
    }


class Registry:
    """能力注册表 + 统一调度（外部调用的单一入口）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        self._tools[spec.name] = spec
        return spec

    def tool(
        self,
        *,
        name: str,
        description: str,
        kind: Literal["read", "write"] = "read",
        reversible: bool = False,
    ) -> Callable:
        """装饰器：把一个函数注册为能力，入参 schema 自动从签名推导。"""

        def deco(fn: Callable) -> Callable:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    handler=fn,
                    kind=kind,
                    reversible=reversible,
                    params=params_from_signature(fn),
                )
            )
            return fn

        return deco

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict]:
        return [t.describe() for t in self._tools.values()]

    def invoke(self, name: str, /, **params: Any) -> dict:
        """调用能力 → 统一信封 dict（异常一律转可教学错误，绝不抛给调用方）。

        ``name`` 是 positional-only（``/``）：否则能力若自带 ``name`` 入参
        （如 add_topic/set_topic_enabled），``invoke("add_topic", name=...)`` 会
        与形参 ``name`` 撞成「重复实参」——通用调度口不能占用能力的参数名。
        """
        spec = self._tools.get(name)
        if spec is None:
            return gate(
                err(
                    "unknown_tool",
                    f"没有能力 '{name}'",
                    hint="先 specs() 或 `paperpilot tools` 看可用能力清单",
                    suggest=self.names(),
                )
            )
        try:
            result = spec.handler(**params)
        except AIError as exc:
            result = {"ok": False, "error": exc.to_dict()}
        except ArxivError as exc:
            result = err(
                "arxiv_unavailable", str(exc),
                hint="arXiv 限速或网络问题：稍后重试，或调小 days/limit",
            )
        except ScholarError as exc:
            result = err(
                "scholar_unavailable", str(exc),
                hint="Semantic Scholar 限速/网络问题：稍后重试或调小 limit；404 表示 S2 无此论文（可换 DOI/CorpusId 或稍后）",
            )
        except TypeError as exc:
            result = err(
                "bad_params", f"参数不匹配: {exc}",
                hint=f"能力 '{name}' 的入参: {list(spec.params)}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("能力 %s 执行失败", name)
            result = err(
                type(exc).__name__, str(exc),
                hint="未分类错误：附上能力名与参数重试；先用只读能力核对输入是否存在",
            )
        if not isinstance(result, dict):
            result = ok(result=result)
        result.setdefault("ok", True)
        return gate(result)
