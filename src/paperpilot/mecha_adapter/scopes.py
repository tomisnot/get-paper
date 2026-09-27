"""作用域策略（**项目侧**）：把命令的 `scope` 声明**接上电**。

## ⚠ 先把这一批的性质说清（免得下一个人读成"补了个大洞"）

**本批绑定在效果上近乎空操作（除 `profile`）** —— 六块作用域两侧都放行。
它的价值在三处：
1. **让那 22 条声明从"装饰"变成"规则"**；
2. **给判据提供那个【必须被拒的反例】**（`profile`）；
3. **以后要收紧时，改的是这张表、不用改代码**。
⇒ **这是把插头插上，不是换了台机器。**

## ⚠ 边界

本模块只负责"**绑上** + 表与意图一致"。判据 `tests/test_scope_policy.py` 的 ①/④/⑥ 能证
"**绑了、表覆盖了所有被声明的 scope、表与意图一致**"，**证不了"意图本身合理"**。

## 背景（为什么需要这个模块）

框架 `CommandRegistry` 里的作用域过滤**只在注入了 `ScopePolicy` 时才生效**：
```python
# mecha/commands.py:432
if self._scope_policy is not None and spec.scope:      # ← 没绑 ⇒ 整段跳过
```
而 `mecha/scopes.py:15` 也自陈"未注入时框架不做 scope 过滤（向后兼容）"。本仓从来没人调过
`bind_scope_policy` ⇒ **声明在、消费者在，插头没插**（实测：`add_topic`（`scope=('topics',)`）
经 **AI 工具面**调用**静默成功**）。⇒ 这正是"框架守卫红不了、只能项目侧判据抓"的那类病
（那一行框架代码**完全合法**、字段有消费者、框架自测也有判据）。
"""

from __future__ import annotations

from mecha.scopes import ScopePolicy

#: 允许表（用户裁决 2026-09-26）：``{side: (scope, …)}``。构造时 `default_allow=False`
#: ⇒ **默认拒绝**（绑定 = 必须逐块表态，而不是"默认全开再逐个关"）。
#: 与判据 `test_scope_policy_table_matches_intent` **互为见证**：改这里就要改那条判据。
_GRANTS: dict[str, tuple[str, ...]] = {
    "human": ("library", "topics", "review", "pipeline", "config", "undo", "profile"),
    # ⭐ `profile` **只给人**：`reset_profile` 是"改自己的标尺"那类动作，它的声明原文就是
    #    "人类专属：不投影给 AI，防自改锚点" —— 这里把那个**意图落成规则**（而不只是注释）。
    #    其余六块两侧都放行：AI 的本职（library）、有对应工具（topics/review/pipeline/config/undo）。
    "ai": ("library", "topics", "review", "pipeline", "config", "undo"),
}


def build_scope_policy() -> ScopePolicy:
    """建出本项目的 `ScopePolicy`；装配期在 `build_commands(...)` 之后绑定它。

    ⚠ **新增命令时**：若它声明了**新的**作用域，必须同时在这里给某一侧授予
    —— 忘了配 ⇒ **运行期会被 fail-closed 拒掉**。判据 ⑥ 会**先红**
    （`test_every_declared_scope_is_granted_to_some_side`）把这件事提前到跑判据时暴露。
    """
    policy = ScopePolicy()
    for side, scopes in _GRANTS.items():
        policy.grant(side, *scopes)
    return policy
