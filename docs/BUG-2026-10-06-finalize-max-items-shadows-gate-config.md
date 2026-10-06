# BUG：`finalize_briefing(max_items=N)` 会整体遮蔽 `set_config` 写的配置（同作者上限等静默失效）

- **发现日期**：2026-10-06（AI 面日报流程实测，非单测暴露）
- **严重度**：P1（用户可见产物静默错误；不损坏数据、可重跑修复）
- **状态**：✅ **已修复**（A + B + D 全做，见 §8）
- **影响面**：`mcp__paperpilot__finalize_briefing` 带 `max_items` 的每一次调用

## 1. 一句话

命令面（N15）会把 `set_config` 写入 Gate 快照的配置注入线程局部覆盖，供流水线读取；
但 `capabilities/tools.py` 的 `finalize_briefing` 在收到 `max_items` 时**又套了一层**
`pipeline_config_override(scoring=settings.scoring.model_copy(update={"max_papers": N}))`——
这层从 **YAML settings** 重新拷贝，把外层 Gate 注入的其余键（`scoring.max_per_author`、
`scoring.must_read_cap`、`scoring.quota_per_topic`…）**全部打回默认值**，且把
`lookback_days` 清空。`read_config` 读的是 Gate 快照所以显示正常，错误只在选择结果里显形。

## 2. 复现（本次实测全过程）

环境：10-06 合并日报，池 53 篇，含同作者（Adolfo del Campo）两篇：`2610.02312`(0.12) 与
`2610.05934`(0.10)，其余篇目最高 0.15、入选线附近 0.06。

```
set_config("scoring.max_papers", 30)      → readback 30 ✓
set_config("scoring.max_per_author", 3)   → readback 3  ✓
finalize_briefing(force=True, max_items=30)
  → selected=30，但 2610.05934(0.10) 被存档；0.06 的篇目反而入选
  ⇒ max_per_author 实际生效值是 settings 里的 1，不是 3

finalize_briefing(force=True)             # 同一快照，不传 max_items
  → 2610.05934 正常入选（第 12 位），del Campo 两篇并存
  ⇒ Gate 快照（30/3）完整生效
```

旁证（说明 MCP 写工具确实走命令面的 Gate 注入）：09-28 曾 `set_config(lookback_days,1)` 后
`prepare_review`，空池诊断打印「lookback_days=1 窗口外」——该诊断读的是
`_eff_lookback_days()`，能读到 1 只可能来自 `_make_handler` 的线程局部注入。

## 3. 根构（三处代码，按调用栈从外到内）

### 3.1 命令面：Gate 快照 → 线程局部覆盖（N15，设计正确）

`src/paperpilot/mecha_adapter/commands.py` `_make_handler`（约 391-394 行）：

```python
scoring, lookback = gate_scoring_override(container, gate)
with pipeline_config_override(scoring=scoring, lookback_days=lookback):
    ...  # 调 capabilities.invoke / surface.run
```

`gate_scoring_override`（`mecha_adapter/engine.py:39-61`）从 Gate 快照构造 ScoringCfg 副本，
**包含全部 `scoring.*` 键**（threshold/quota_per_topic/max_papers/max_per_author/must_read_cap）。
注释原话："prepare/submit/finalize 各段同样吃到，杜绝静默 no-op"。

### 3.2 能力面：`max_items` 分支再从 settings 重建覆盖（BUG 所在）

`src/paperpilot/capabilities/tools.py` `finalize_briefing`（约 1013-1021 行）：

```python
from ..domain.pipeline import pipeline_config_override
if max_items:
    # N12 同族：显式篇数 > 配置（gate/yaml），只活在本次调用的线程局部覆盖里
    with pipeline_config_override(scoring=settings.scoring.model_copy(
            update={"max_papers": int(max_items)})):
        result = pipeline.finalize_review(...)
```

问题两点：
1. **基准对象错**：`settings.scoring` 是 YAML/进程级配置，不是"当前生效配置"。
   嵌套在外层 Gate 覆盖之内执行时，它把 Gate 注入的 scoring 整体换掉。
2. **顺手清掉 lookback**：内层不传 `lookback_days`（默认 None），而上下文管理器对 None
   是"无条件清空"（见 3.3）⇒ 执行期间 `_eff_lookback_days()` 回落 settings 值。

### 3.3 上下文管理器：替换语义 + None 即清空（隐患面）

`src/paperpilot/domain/pipeline.py:71-87`：

```python
prev_scoring = getattr(_CONFIG_OVERRIDE, "scoring", None)
prev_lookback = getattr(_CONFIG_OVERRIDE, "lookback_days", None)
_CONFIG_OVERRIDE.scoring = scoring            # 无条件赋值，None 也赋
_CONFIG_OVERRIDE.lookback_days = lookback_days
```

docstring 写 "None = 不覆盖"，但实现是"None = 清空该字段"——仅在最外层调用时二者等价，
**嵌套时清空会波及外层**。读取侧 `_eff_scoring()/_eff_lookback_days()`（同文件 134-143 行）
对 None 一律回落 `self.settings`，所以遮蔽全程无日志、无告警。

## 4. 影响面

- 带 `max_items` 调 `finalize_briefing` 时，以下经 `set_config` 的设置**静默失效**：
  `scoring.max_per_author`、`scoring.must_read_cap`、`scoring.quota_per_topic`、
  `scoring.threshold`（已废弃但仍会误导）、`lookback_days`（finalize 不读，属潜伏）。
- 反向也错：若 YAML settings 与 Gate 快照不一致（重启后必然不一致——重启回 YAML），
  行为以 YAML 为准而非用户最后一次 `set_config`，与"显式意图 > 配置"的总纲相悖。
- 同类雷区：任何能力若照 `finalize_briefing` 这个写法自建 `pipeline_config_override`，
  都会复炸。当前仅此一处（`grep pipeline_config_override` 三个调用点：commands.py:394、
  engine.py:104、tools.py:1018）。

## 5. 修法（建议 A 必做；B 建议一并做，防同族复发）

### A. 能力面：从"当前生效配置"派生，而不是从 settings（最小修复）

`capabilities/tools.py` max_items 分支：

```python
if max_items:
    base = pipeline.effective_scoring()        # 建议在 domain/pipeline.py 暴露公开访问器
    with pipeline_config_override(
            scoring=base.model_copy(update={"max_papers": int(max_items)}),
            lookback_days=pipeline.effective_lookback_days()):
        result = pipeline.finalize_review(...)
```

语义保持 N12 原意（`max_items` 只赢 `max_papers` 这一个键），其余键继承当前生效值
（Gate 快照/外层覆盖）。`effective_scoring()` 就是把现有私有 `_eff_scoring()` 提公开。

### B. 框架面：让上下文管理器"合并"而非"替换"（根治同族）

```python
@contextmanager
def pipeline_config_override(*, scoring=None, lookback_days=None):
    prev = (getattr(_CONFIG_OVERRIDE, "scoring", None),
            getattr(_CONFIG_OVERRIDE, "lookback_days", None))
    if scoring is not None:
        _CONFIG_OVERRIDE.scoring = scoring
    if lookback_days is not None:
        _CONFIG_OVERRIDE.lookback_days = lookback_days
    try:
        yield
    finally:
        _CONFIG_OVERRIDE.scoring, _CONFIG_OVERRIDE.lookback_days = prev
```

docstring "None = 不覆盖"自此成立。现存三个调用点都传实值，无人依赖 None 清空，安全。
注意：**单做 B 不修本 bug**（内层传的是 scoring 对象仍会替换）；A 才是本 bug 的修复，
B 是防 `lookback_days` 被顺手清掉及未来同族错误。

### C.（可选）给个 delta 形态，从 API 上消灭"从 settings 重建"的诱惑

```python
with pipeline_config_override(update={"max_papers": int(max_items)}):  # 在生效值上叠加
```

### D. 回归判据（建议进 tests/，与 test_journal.py 同风格）

`test_finalize_max_items_inherits_gate_config`：
1. `gate.set("scoring.max_per_author", 3)`（或走 set_config 工具）；
2. 构造/取用含同作者两篇（分数 0.12/0.10）+ 若干低分篇的池；
3. 以 `max_items=N` 调 finalize；
4. **断言两篇同作者论文同时入选**，且不得有更高分论文因作者规则被挤掉；
5. 附加断言：执行期间 `_eff_lookback_days()` 等于 Gate 值而非 settings 值。

更一般的不变式（值得写成参数化判据）：**对任意写命令，`set_config` 写的每个键在该命令
执行期间都必须可见，除非该命令的显式参数正好覆盖那一个键。**

## 6. 本期变通办法（可复现路径，供追溯）

本期 10-06 合并日报最终版的做法：不传 `max_items`，改为
`set_config("scoring.max_papers", 30)` + `set_config("scoring.max_per_author", 3)` 后
`finalize_briefing(force=True)`——Gate 快照经命令面完整生效，结果正确（del Campo 两篇
并存入选）。**这是绕坑，不是修复**；修复后应恢复"显式篇数走 max_items"的用法，并把以上
不变式交给判据钉住。

## 7. 受影响的历史产物

- 10-06 简报第 23、24 版（briefing_id 23/24）是在 `max_per_author=1`（被遮蔽）下选出的，
  已由第 25 版（不带 max_items 重跑）覆盖修正，现行 `/digest/2026-10-06` 为正确版本。
- 更早的 20 篇/12 篇日报未同时使用 `set_config` 改过其他 scoring 键，不受影响。

## 8. 修复记录

按 §5 的 A + B + D 全做（C 的 delta 形态未做——A+B 已覆盖，无实际调用方需要）：

**A（本 bug 的修复）** `capabilities/tools.py::finalize_briefing`
```python
base = pipeline.effective_scoring()          # ← 从"当前生效值"派生，不再从 YAML 重建
with pipeline_config_override(
        scoring=base.model_copy(update={"max_papers": int(max_items)}),
        lookback_days=pipeline.effective_lookback_days()):
```
配套：`domain/pipeline.py` 把私有 `_eff_scoring`/`_eff_lookback_days` **提公开**为
`effective_scoring()`/`effective_lookback_days()`（5 处内部调用点一并改名）——公开访问器是
"能力层在生效值上叠单个键"的**唯一正确入口**。

**B（根治同族）** `pipeline_config_override` 由"无条件赋值"改为**合并**：
`None` = **不动这个字段**，docstring 的"None = 不覆盖"自此与实现一致。现存三个调用点
（commands.py / engine.py / tools.py）都传实值，无行为回归。

**D（回归判据）** 新增 `check/test_config_override_nesting.py` 三则：
1. `test_nested_override_merges_instead_of_clearing` —— 内层不传 lookback 不许清掉外层（B）；
2. `test_finalize_max_items_only_wins_its_own_key` —— **走真实命令面** `set_config` 写 Gate 快照，
   再以 `max_items` 定稿，**在调用期间抓取生效配置**：`max_papers`＝显式值，而
   `max_per_author`/`must_read_cap` 必须继承 Gate 值（BUG 时会掉回 YAML 默认）；
3. `test_no_capability_rebuilds_config_from_settings` —— **结构性守卫**：走 AST 查能力层里
   有没有 `settings.scoring.….model_copy` 这种"从 YAML 重建"的形状（拦的就是本 bug 的成因）。

> 判据落在 **`check/`**（必要判据、随仓发布），不是本文原稿写的 `tests/`——那批之后
> 项目把"判据"与"普通测试"分了目录。

**"能红"验证**（不是只跑绿）：把 A 的修复临时改回旧写法重跑 ⇒ **两条判据当场红**
（行为那条 + 结构守卫），还原后恢复绿。判据确实咬得住这个 bug，不是摆设。

**§6 的变通办法就此退休**：`max_items` 可以放心与 `set_config` 同用。
