# PaperPilot 缺口清单与补齐方案

> 审计日期：2026-09-21。参照系：Energy Level 项目积累的 AI 接入框架经验（"七层脊椎"，§0）。
>
> **结论一句话**：PaperPilot 忠实复刻了 Energy Level 的「MCP 纪律层 + dsh 集成层」，但**没有接上
> "语义脊椎"**——唯一 choke point 的归因、append-only 记录仪、undo、监控投影、编排面。
> 对本域（个人自用 / 低风险 / 可重跑 / 无硬件）多数缺口可接受；**真正会在意的是归因与记录仪**
> （"确保不被 AI 糊弄"的证据链）。本文给出每条缺口的证据、影响与**薄补齐**方案（不重构）。

---

## 〇、已解决（2026-09-21 本轮补齐）

> P0-A / P0-B / P1 / P2 已全部落地并通过三条判据测试（`tests/test_journal.py` 13 例）。
> 正文各条若与现状不符，以本段 + 代码为准。

- **P0-A 归因**：`infra/repo.py` 所有写入手动接受 `*, actor, reason` 关键字，并在**同一事务内**
  append 事件。actor 约定：MCP 工具="ai"（`ACTOR`）、Web/CLI="human"、调度="scheduler"、
  内部默认="system"/"pipeline"。MCP 写入工具全部新增可选 `reason` 参数。
- **P0-B 记录仪**：新表 `events`（seq/ts/actor/reason/op/target/before/after/reversible），
  **append-only 由 DB 触发器钉死**（`events_no_update` / `events_no_delete`，UPDATE/DELETE 即
  ABORT——判据②）。读面：`repo.events_since(since_seq/actor/op)` + MCP 工具 `get_activity`
  （events 为主答案，diff-since-seq + 过滤 + 体积闸）+ Web 只读页 `/activity`（记录仪）。
- **P1 undo**：`repo.undo(seq=0)` 按事件的 before 快照回写（阅读态/笔记/主题同步/状态重置），
  MCP 工具 `undo(seq, reason)`；不可逆 op（`upsert_papers`/`save_briefing`）标 `reversible=0`，
  undo 时抛可教学错误（kind=irreversible，提示"简报可重跑生成新版本"）；undo 本身也记一条
  `reversible=0` 事件（防循环）。
- **P2 可教学兜底**：`_safe` 的 catch-all except 现在回 `{kind, message, hint}`，
  hint 指导"附调用与参数重试，或先用 search_papers/get_paper/list_topics 核对输入"。
- **踩到并修掉的两个真 bug**（记录仪自身的）：
  1. `_reading_event` 原在变更**之后**拍 before 快照 → before==after，undo 回写的是新值
     （undo 静默失效）。修：`_reading_snapshot()` 在变更前拍。
  2. `sync_topics` 无变化也记事件 → 记录仪噪声。修：只记**真实发生**的状态变更
     （对齐 mecha SKILL.md"journal 只记成功的状态变更"）。
  3. `AIError` 的 kind 只能靠类属性，临时 kind（nothing_to_undo/irreversible）落进 extra。
     修：构造器接受 `kind=` 覆盖。

**验收（判据测试 `tests/test_journal.py`）**：① 每条写入带 actor/reason（13 例覆盖
repo/MCP/pipeline 三条路径）；② `UPDATE/DELETE events` 抛 `SQLAlchemyError`（触发器）；
③ undo 往返一致（收藏/笔记/指定 seq），不可逆 op 明确拒绝。全量 **71 passed + ruff 零告警**。

---

## 0. 参照系：七层脊椎（一句话版）

| 层 | 内容 |
| --- | --- |
| L0 语义状态层 | 单一真值源；旋钮 typed+单位+值域+可逆性+风险；schema 唯一定义点 |
| L1 唯一 choke point | 所有写入过一个喉点，**无侧门** |
| L2 append-only 事件总线 | actor/reason/before/after/可逆性/seq + undo（"不被糊弄"的证据链） |
| L3 工具面=语义层薄封装 | 可教学错误 / 体积闸 / 写后 readback / 边界显式 / 真实签名推 schema |
| L4 编排面 | CodeAct 在语义层上自由组合 + 声明式批处理 + 长任务 async |
| L5 监控面=总线三投影 | HUD / 告警 / 记录仪；黑匣子读文件不读活进程 |
| L6 强制层 | 分级自治；不可逆=AI 提议+人确认+硬件互锁；AI 不进硬实时 |
| L7 可复现+自检 | 结果自带复现配置；判据 claim-vs-measured；回归全绿才切默认 |

---

## 1. 已符合（**不要误改**，这些是做对了的）

- **MCP 纪律**（`mcp_server.py`）：可教学错误(kind/hint/suggest)、回程体积闸 `_gate`(kept/total/where)、
  人机同路径（MCP/CLI/Web 调同一批 service）、`.mcp-port` 端口文件发现（T4：harness 绝不拉起权威）、
  localhost-only、工具=service 薄封装（不含业务逻辑副本）、真实签名推输入 schema。
- **dsh 集成**（`dsh/`）：自愈 MCP 桥（并**修复了 Energy Level 原版 `ensureReady` 的一个缺陷**）、
  两阶段工具 swap 注册、client 简报面板、launcher `paperpilot ai`。
- **契约与降级**：`ports/ai.py` 冻结 + pydantic 强校验 + `json_mode` 解析失败必抛错；
  AI 是"奢侈品"（heuristic/off 兜底、简报标 `ai=false`，**不静默降级**）。
- **可复现/幂等**：run_id + 唯一约束防重、简报不可变（重跑生成新 run、旧 briefing `superseded`）、
  `ai_calls` 记账、topics 以 `settings.yaml` 为单一事实源。
- **自检**：58 pytest 全绿 + ruff 零告警 + dsh 14/14 + MCP 活体协议测试通过。

---

## 2. 缺口 P0-A：归因没接线

- **证据**：`mcp_server.py:46` 的 `ACTOR = "ai"` **定义后全文未用**；`mark_read / star_paper /
  skip_paper / add_note / add_topic / set_topic_enabled / fetch_papers` 等写入均不记 actor/reason。
- **影响**：无法区分某条笔记/收藏/主题/抓取是 **AI、人（Web/CLI）还是调度器**写的；L2 归因链缺失，
  "不被糊弄"退化为"信任当前 DB 态"。
- **补齐（薄）**：
  1. `infra/repo.py` 是唯一写入收敛点 → 其写入方法统一加关键字 `*, actor: str, reason: str = ""`（改面小）。
  2. 调用方传 actor：MCP 工具传现成的 `ACTOR`("ai")；CLI/Web 传 `"human"`；scheduler 传 `"scheduler"`。
  3. 每次写入在事务内 append 一条事件（见 §3 的 `events` 表），before/after 记关键字段快照。
- **验收**：任意一条 note/star/topic 能回答"谁、何时、为什么"改的。

## 3. 缺口 P0-B：无 append-only 记录仪 / 黑匣子

- **证据**：全项目无 journal / event_log / append-only（grep 无命中）；历史只有 `ai_calls`（AI 调用记账）
  + `runs`（运行）+ 当前 DB 态；"AI 到底改了什么"的**逐 op 证据链不可重建**；监控是 Web 渲染 +
  `get_activity`，不是总线投影；DB 是**活状态**不是日志。
- **补齐（薄）**：
  1. 新表 `events`（**append-only：只 INSERT，不 UPDATE/DELETE**）：
     `seq INTEGER PK AUTOINCREMENT, ts, actor, reason, op TEXT, target TEXT,
     before JSON, after JSON, reversible INTEGER`。
  2. repo 每个写入方法在事务内 append 一条。
  3. `get_activity` 扩展为可读 events（按 actor/op 过滤 + 分页 + 体积闸）；可选加"记录仪"只读页
     （Web 页签或 dsh 面板页签）。
  4. 若要做到"进程死也能读过程"（黑匣子寿命独立），可另落一份 jsonl 文件；否则 DB 持久化程度即寿命。
- **验收**：杀进程后仍能读出完整 op 序列；试图 UPDATE/DELETE events 应失败（判据钉死）。

## 4. 缺口 P1：无 undo / 可逆性标记

- **证据**：无 undo / reversib 任何痕迹；`mark_read/star/skip/add_note/add_topic` 不可撤销。
- **补齐（薄）**：§3 的 `events.before` 已存快照 → 加 `repo.undo(seq)`（按 before 回写）+ MCP 工具
  `undo_last` / `undo(seq)`；对不可逆 op（如 `finalize_briefing`）标 `reversible=0`，undo 时**拒绝并给
  可教学错误**（而非静默）。
- **验收**：AI 误加一条 note 可一条命令撤销；不可逆 op 明确拒绝。

## 5. 缺口 P2：catch-all 错误不可教学

- **证据**：`mcp_server.py:_safe` 的兜底 `except Exception` 只回 `{kind, message}`，**无 hint/suggest**
  （仅 `AIError`/`ArxivError` 带）。
- **补齐**：兜底体加一句 `hint`（如"未分类错误：附调用与参数重试，或先用 search_papers/get_paper 核对输入"），
  traceback 仍进日志；保持不 500。
- **验收**：任何未分类错误回程也带 hint。

## 6. 缺口 P3（可选）：无编排面（CodeAct）

- **证据**：AI 只有 18 个原子工具 + 固定三段评审协议（prepare/submit/finalize）；不能 ad-hoc 多步组合
  （如"检索+交叉过滤+聚合+批注"一次完成）。表达自由度被 capped。
- **为何可接受**：本域"流水线即编排"（三段协议本身就是声明式协作），且无物理风险、探索性组合需求低。
- **若要做（薄）**：加 `paperpilot_script` 工具，命名空间只暴露 `repo/retrieval/pipeline/settings` 的
  **读门面 + 已验证写门面**（表达自由度拉满、契约自由度锁死），禁 `open/eval/__import__`；产物赋
  `result`、`print` 捕获。参照 Energy Level `src/mecha/codeact.py`。
- **验收**：一次调用完成"搜→筛→批注"，且每步写仍过 §2/§3 的归因+journal。

## 7. 优先级与顺序

1. **P0-A + P0-B 一起做**（归因 + journal 同属一层，repo 只改一次）。✅ 已完成
2. **P1 undo**（依赖 events.before 快照）。✅ 已完成
3. **P2** catch-all hint。✅ 已完成
4. **P3** 编排面（可选）。⬜ 待办

补齐后已加判据（`tests/test_journal.py`）：① 每条写入带 actor/reason；② `events` 只增不改
（UPDATE/DELETE 判据失败）；③ 可逆 op 的 undo 往返一致。

---
