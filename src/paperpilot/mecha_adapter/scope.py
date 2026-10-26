"""作用域与写权。

作用域是**不透明字符串**（框架不解释），这里按动作类别分组：

| scope | 覆盖 |
|---|---|
| `library` | 收藏/已读/跳过/笔记/写卡/信号 |
| `fetch`   | 抓 arXiv、按编号拉单篇 |
| `drafts`  | 建/改/删草稿 |
| `profile` | 重算权重、增删锚点 |
| `citations` | 同步引文边 |
| `config`  | 改可写配置 |

出厂**两侧全授予**：这个软件的设计就是"人与 AI 共用写权"（`Mode.OPEN`），
作用域的价值不在今天拦谁，而在**声明存在、机制接上**——将来要收紧，
只改这一张表，不用动命令面。
"""

from __future__ import annotations

from mecha.scopes import ScopePolicy

SCOPES = ("library", "fetch", "drafts", "profile", "citations", "config")


def build_scope_policy(*, allow: tuple[str, ...] = SCOPES) -> ScopePolicy:
    policy = ScopePolicy(default_allow=False)
    policy.grant("human", *allow)
    policy.grant("ai", *allow)
    return policy
