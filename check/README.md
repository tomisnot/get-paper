# check/ —— 必要判据

与 `tests/` 的分工：

| 目录 | 是什么 | 增删纪律 |
| --- | --- | --- |
| **`check/`** | **判据**：不许烂的守卫。每条都在说"这件事一旦坏了，系统就是在骗人/不安全" | 改产品行为要**同步改**它；改不动就先说明**为什么有意改判据**——不许悄悄删 |
| `tests/` | 普通测试：单元、契约、回放、集成 | 随实现自由增删 |

## 现在有哪些判据

- `test_skill_sync.py` —— skill 与工具注册表**双向**同步（AI 不知道的工具＝不会用；skill 里的幽灵名＝过时文档）
- `test_capability_map.py` —— **声明 == 投影**（能力侧与工具面两侧逐字对齐，含人类专属的显式扣除）
- `test_mecha_criteria.py` —— 工具面判据：投影恰一次、参数机械派生、工具"列得出也调得动"
- `test_scope_policy.py` —— 作用域允许表与意图一致（`profile`/`views`/`marks` 只给人）
- `test_telemetry_kinds.py` —— 能力的 `kind` 与它实际留下的痕迹**对账**（读不留痕、遥测只留遥测）
- `test_journal.py` —— append-only 事件总线（库层触发器禁止 UPDATE/DELETE）
- `test_ai_experience.py` —— 统一信封纪律 + 参数描述齐备（N8）
- `test_graph_base.py` / `test_graph_view.py` —— 图底座几何与视图面编排（含"点不许被排出画布"）
- `test_reading.py` —— 精读体系：消毒、块清单、精确锚定、歧义、截断游标、删除不投影

## 跑法

```bash
.venv\Scripts\python -m pytest check -q      # 只跑判据（改动后的第一道闸）
.venv\Scripts\python -m pytest -q            # 判据 + 普通测试（pyproject 的 testpaths 两者都含）
```
