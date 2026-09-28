"""skill↔工具面 防脱轨判据（write_note 事故的制度化回应）。

方向一：每个真实工具名必须出现在 SKILL.md（AI 不知道=不会用）。
方向二：skill 反引号里 snake_case 的"工具样 token"必须是真实存在的能力/参数/白名单
        ——跑在前面的文档就是幽灵（上一位受害者：write_note 当摘要）。
名单从注册表派生，不手抄：改工具不改文档 ⇒ 这里红。
"""

from __future__ import annotations

import re
from pathlib import Path

from paperpilot.capabilities.tools import PARAM_DESCRIPTIONS
from paperpilot.mecha_adapter.tools import TOOL_DECLS

SKILL = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "paperpilot" / "SKILL.md"

# tests/test_mecha_adapter.py 的同名常量在这里独立取，避免测试间耦合
NONCAP = {"set_config", "read_config", "read_authority",
          "submit_job", "read_job", "cancel_job"}


def _tool_names() -> set[str]:
    return {d.mecha_name for d in TOOL_DECLS} | NONCAP


def test_every_tool_is_documented_in_skill():
    text = SKILL.read_text(encoding="utf-8")
    tools = _tool_names()
    missing = sorted(t for t in tools if f"`{t}`" not in text)
    assert not missing, f"这些工具 skill 没入册，AI 不会知道去找谁：{missing}"
    assert len(tools) >= 42


def test_skill_has_no_ghost_tool_names():
    text = SKILL.read_text(encoding="utf-8")
    tokens = {m for m in re.findall(r"`([a-z][a-z0-9_]{2,})`", text) if "_" in m}
    real = (_tool_names()                                      # 42 工具
            | set(PARAM_DESCRIPTIONS)                          # 能力名
            | {p for d in PARAM_DESCRIPTIONS.values() for p in d}  # 参数名
            | {"reset_profile", "arxiv_id", "read_telemetry", "must_read",
               "next_offset", "review_floor", "max_papers", "max_per_author",
               "must_read_cap", "quota_per_topic", "in_briefing"})
    ghosts = sorted(tokens - real)
    assert not ghosts, f"skill 引用了不存在的工具样名字（幽灵/过时）：{ghosts}"
