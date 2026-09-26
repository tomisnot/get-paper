"""EXP-4 · AI 面守卫（命名/描述纪律）——把"改对后的模型可见面"钉死。

按内容扫全部 28 个工具的名字与描述（不看 diff）：命名 snake_case 动词_宾语、禁
get_/list_ 前缀、描述不含实现词汇（UI/传输/内部结构）。红证：引入一个 get_ 前缀或
描述写"sqlite"，本守卫即红。
"""

from __future__ import annotations

import re

from .test_mecha_adapter import EXPECTED_TOOLS, _stack

#: mecha `_NAME_RE` 同款：小写、≥2 段、动词_宾语。
_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")

#: 描述里不许出现的实现词汇（任务语言 vs 实现语言的边界）。
_IMPL_WORDS = (
    "sqlite", "sqlalchemy", "mcp", "fts", "yaml", "journal", "append-only",
    "container", "repo", "gate", "transport", "rpc", "iframe", "socket",
    "port", "web panel", "browser",
)


def _schemas(tmp_path) -> list[dict]:
    _c, stack = _stack(tmp_path)
    return stack["tools"].schemas()


def test_all_tool_names_are_valid_snake_verb_object(tmp_path):
    """28 工具名全合 snake_case 动词_宾语，且无 get_/list_ 禁前缀。"""
    names = {s["name"] for s in _schemas(tmp_path)}
    assert names == EXPECTED_TOOLS, "投影名集与期望不符（声明==投影在另一判据细验）"
    for n in sorted(names):
        assert _NAME_RE.match(n), f"工具名 {n!r} 不合 snake_case 动词_宾语（≥2段）"
        assert not n.startswith(("get_", "list_")), f"{n} 用了框架禁的 get_/list_ 前缀"


def test_tool_descriptions_carry_no_implementation_words(tmp_path):
    """描述只讲任务概念，不外泄实现词汇（L4：描述是模型的 UI）。"""
    checked = 0
    for s in _schemas(tmp_path):
        desc = str(s.get("description", ""))
        low = desc.lower()
        leaked = [w for w in _IMPL_WORDS if w in low]
        assert not leaked, f"{s['name']} 描述含实现词汇 {leaked}：{desc!r}"
        assert desc.strip(), f"{s['name']} 描述为空"
        checked += 1
    assert checked == len(EXPECTED_TOOLS)          # R8：确实逐个扫过，非空表假绿
