# 说明：history 那事，run 这事（给《Get Paper》下一代的开发者）

> **这份文档存在的唯一理由**：把两次**认知错误**钉在纸上——免得后来人重犯，更别照抄。
> 它**不是**本仓的运转手册；仓怎么转看 [`交接-给下一代.md`](交接-给下一代.md)。
>
> 读法：5 分钟，两节各一件事。**先读 §0 的结论，再看推演。**
> 本文若与代码不符，是本文的 bug——请对着符号名核实后改本文。

---

## 0. 一句话结论

本仓有两条**根子上错了**的路，都是**接入 mecha 之前**自造的：

1. **history 那事**：本仓自建了一套 append-only 事件账（`repo.events` + DB 触发器），
   与 mecha 的 `History` **并行成了"双账"**。它**能工作**，但两条流**可能分叉**，
   `/activity` 要做两区互引来"防"分叉——这是**补丁**，不是结构。
   ⇒ 框架后来给了 **fold 注入**，两本账可以**收成一本**（§1）。

2. **run 这事**：本仓把"**软件自己调 LLM 把 AI 活干完**"做成了 `run` 这条路
   （`infra/ai/unified.py` 的 `UnifiedAIAdapter`「程序化辅轨」+ `Engine.run` 内嵌 LLM）。
   **这从根上误解了 mecha 的定位**——软件只提供"能力 / 记忆 / 治理"，智能在
   **AI 适配器（dsh）** 侧。这条路**从来没有走通**，且用户此前**不知道它的存在**
   （发现后判定"早该删"）。⇒ **别再照这条路往下修；该退役**（§2）。

> 两条路的共同教训（§3）：**mecha 里，软件不生成内容、不接模型；智能在适配器侧。**

---

## 1. history 那事：双账，与 fold 注入的收编

### 1.1 事情怎么来的（时间线）

本仓先于 mecha 而存在一个**自己的事件账**：
`repo.events`（域事件："论文被收藏 / 笔记被加 / 收藏被撤"）+ 数据库触发器钉死"只增不改"
（`infra/db.py` 的 `init_db` 里那几条 `ABORT` 触发器）。

接入 mecha 后，框架带来**第二本账**：`History`（框架审计 `command.<name>` + 配置态 KV）。
当时的做法是本仓的**选择**——**让 mecha 把账写进本仓自己的库**：
装配时 `log_sink=container.repo.ledger`（见 `mecha_adapter/hub.py` 的 `build_stack`），
落地点是 `infra/ledger.py` 的 `SqlLedger`。

### 1.2 做对了什么

`SqlLedger` 有两条**真的对**的性质，**别弄丢**：

- **账住在状态旁边**：mecha 的账落进本仓的 `events` 表 ⇒ **同库**；
- **同事务**：`SqlLedger.append` 发现"在途 session"就跟着调用方一起 commit
  （`infra/ledger.py` 的 `ledger_session`），⇒ **"状态变了、账不可能没记"**
  ⇒ 域状态与账**不可能一个有一个没有**。

> 这条是通用原则（框架也认）：**账必须与域状态变更同事务**；能同库同事务 ⇒ 就不需要第二本账。

### 1.3 错在哪：它**仍然是两条流**

物理上同表、同序列（`seq` 由数据库自增分配，`SqlLedger.last_seq` 续接），
但**逻辑上是两条路**：

- **域事件**由 `repo.*` / `repo.record_op` 写（"数据变成了什么"）；
- **框架审计**由 `SqlLedger.append` 写（"谁执行了一次命令"）。

于是**监控面是分裂的**：`app/web.py` 的 `/activity` 页要把两本账**并排成两区**，
靠命令审计里的**不透明 `result_ref`**（`arxiv_id` / `note_id` / `date` / `run_id`）把两区互引。

> ⚠ **互引是"防分叉"，不是"结构上不可能分叉"。** 两条流只要各有各的写口，
> 就有"一个写了一个没写"的窗口——互引只能事后对照，不能事前保证。

### 1.4 正确的解法：fold 注入（框架已给）

框架 ADR
`mecha/docs/v2/notes/implemented/2026-10-12-History-fold注入-从配置账到通用域状态史.md`
把 `History` 从"配置账"升格为**通用域状态史**：`History` 接受一个**可选的 `fold` 构造参数**，
`snapshot()` 改调注入的 fold（默认仍是"后写覆盖"，现有消费方零影响）。

对本仓的意义：

- 本仓的**域事件可以成为 `History` 的主体**（一等公民，不再是"审计附件"）；
- 本仓传一个 fold = "**把事件物化进 SQLite**"（把 `repo` 的状态从事件算出）；
- ⇒ **双账可以收成一本**；`/activity` 不必再做两区互引——账本来就是**一条流**。

⚠ **边界**：fold 注入只解决"**快照怎么从账算出**"，**不改变**"**写必须过 Gate**"。
域事件仍要经命令面（`side_effect=True`）走写权门 + 审计。

### 1.5 现状与给未来开发者

- **现状**：本仓**还没**用 fold 注入（`History` 未传 fold）；现在是"同表同序列"的**过渡形态**，
  `/activity` 仍是两区互补（`mecha_adapter/monitor.py` 的注释称之为 "Scheme E：两份 journal 互补、非替换"）。
- **这是开放选项，不是欠债清单**：真要收编时，改法是"域事件进 `History` + 注入 fold 物化进 SQLite"，
  然后拆掉 `/activity` 的两区互引。
- **别做**：不要再往"互引"上加更多对照逻辑——那是在给补丁打补丁。

---

## 2. run 这事：程序化 LLM 后端，是接入前的定位误解

### 2.1 那条"run 路"是什么

在 `docs/DESIGN.md` §17 里，本仓记了一条**自己发明的**路：

```
paperpilot run（CLI）或 AI 调 run_pipeline
 └─ mecha Surface.run(...)
     └─ PaperPilotEngine.run            ← mecha_adapter/engine.py
         └─ capabilities.invoke("run_pipeline")
             └─ DailyPipelineService.run        ← domain/pipeline.py
                 ├─ 规则过滤 + 打分（infra/ai/*；无 key 时 heuristic）
                 └─ 渲染简报 …
```

其中 `infra/ai/` 是**程序化 LLM 后端**：

- `domain/ports/ai.py` 的 `LLMPort`（"统一 AI 框架适配器的最小接口"）；
- `infra/ai/unified.py` 的 **`UnifiedAIAdapter`**——一个 **OpenAI 兼容 `/chat/completions` 客户端**，
  需要 API key（`PAPERPILOT_API_KEY` / `DEEPSEEK_API_KEY`），在软件**内部**直接调模型；
- `infra/ai/llm_based.py`（默认 LLM 组合）、`infra/ai/heuristic.py`（无 key 时的伪 AI 兜底）。

DESIGN.md §17 把它叫"**程序化辅轨**（供无对话的程序化使用）"，与 DSH/MCP"主轨"并列——
**"AI 两轨而非单轨"**（§17.2 第 4 条）。

### 2.2 为什么错：它误解了 mecha 的定位

mecha 的核心是"**人机同门**"：

- **软件** = 能力面（Surface）+ 账（History）+ 治理（Gate/Authority）+ 监控（Monitor）。
- **AI 是平级的操作者适配器**（今天是 dsh），**和 GUI 一样过同一道门、记同一本账**。

⇒ 软件里**内嵌**一个"主动调模型"的客户端（`UnifiedAIAdapter`），等于**把"智能"焊进了软件**。
这恰好是 mecha 要**分开**的两样东西被硬捏在一起：

| 归属 | 该放哪 | 本仓那条 run 路放错了什么 |
|---|---|---|
| **能力 / 记忆 / 治理** | 软件（本仓） | ✅ 这部分对 |
| **智能（判断、生成）** | **AI 适配器（dsh）** | ❌ 被塞进了 `infra/ai/unified.py`（软件内部） |

**正确的内容生产**：dsh 里的 AI **经 MCP 工具**去做。本仓**已经有**这条正道——
`domain/pipeline.py` 的 `prepare_review / submit_review / finalize_review`
（"AI 经 MCP 分段协作"，见 DESIGN.md §17.1）。**那条才是主轨。**

### 2.3 为什么说它"从来没走通"

不是因为某个 bug，而是**从根上就不该有**：

- 需要 API key ⇒ **无 key 时降级 heuristic**（关键词重合 + 首句摘要）——
  所谓"AI 两轨"，其实是让**软件**替 AI 背了一个不属于它的包袱；
- 与 MCP 主轨**并存** ⇒ 两套 AI 路径 ⇒ **"内容到底是谁生产的"说不清**，
  账与监控面对"这条简报是软件自己算的、还是 dsh 的 AI 写的"无法归因；
- 用户从**未授权**、**此前不知其存在**——发现后直接判"**早该删**"。

> ⚠ 注意区分：**`Engine.run` 这个框架概念本身不稀奇**（mecha 的 `Engine` 四必实现之一；
> 没有"运行概念"的项目可以**显式声明 skipped**，如 tutor 项目就这么干）。
> **错的是让 `run` 去调 LLM**——把"运行"和"智能"绑在了一起。

### 2.4 现状与建议（⚠ 待人类裁决）

- **现状**：这条路**仍在仓里**（`infra/ai/unified.py`、`llm_based.py`、`LLMPort`、
  `Engine.run` 对 `infra/ai/*` 的使用，以及 `ai.provider: auto|unified|heuristic|off` 配置档）。
- **建议**：**退役 / 删除**"软件自带程序化 LLM 后端"这条路；AI 的活**一律走 dsh/MCP**。
  留下来的"无 key 兜底"若要保留，也必须**如实标注它不是 AI**（不是"降级 AI"，是"没有 AI"）。
- **⚠ 这是人的决定，不是顺手重构**：动它之前**先问用户**。
  （用户已表态"早该删"，但**删到什么程度、留不留 heuristic 兜底**，要用户拍板。）

---

## 3. 教训（可迁移到任何 mecha 子项目）

这三条**不限于本仓**；后来做 `tutor` 项目时，就因为它**照抄了本条 §2 的错**
（做了 `llm_distiller` 的"后端协议注入"），又踩了一次同一个坑。

1. **软件不生成内容、不接模型。** 不 import 模型客户端、不持 key、不内置 prompt。
2. **智能在适配器侧。** 软件只暴露三件事：**给原料 / 收结果 / 留痕**。
   （协作形态：`prepare_*`（读，给原料）→ AI 判 → `submit_*`（写，判断进账）。）
3. **状态与账同住、同事务；账能折叠出状态。** 用框架的 `fold` 注入，
   别再自造第二本账去"互引"。
4. **铲错误要铲根。** 发现做错了，**别把歪头砍掉再长一个**——要问"这条路的根认知错在哪"，
   然后立一条**结构上的红线**（如"不准 import 模型客户端"），而不是换个写法重来。

---

## 附：本文的证据落点（按符号名 grep，别按行号）

| 说法 | 去哪核实 |
|---|---|
| 双账：域事件 + 框架审计同表 | `infra/ledger.py`（`SqlLedger` / `ledger_session`）、`mecha_adapter/hub.py:build_stack` 的 `log_sink=` |
| `/activity` 两区互补（Scheme E） | `mecha_adapter/monitor.py` 模块 docstring |
| fold 注入 ADR | `mecha/docs/v2/notes/implemented/2026-10-12-History-fold注入-从配置账到通用域状态史.md` |
| 程序化 LLM 后端 | `domain/ports/ai.py`（`LLMPort`）、`infra/ai/unified.py`（`UnifiedAIAdapter`）、`infra/ai/llm_based.py`、`infra/ai/heuristic.py` |
| `run` 路 = 每日流水线 | `mecha_adapter/engine.py`（`PaperPilotEngine.run`）、`domain/pipeline.py`（`DailyPipelineService.run`） |
| 主轨：AI 经 MCP 分段协作 | `domain/pipeline.py` 的 `prepare_review / submit_review / finalize_review` |
| "程序化辅轨 / AI 两轨"的来源自述 | `docs/DESIGN.md` §17（§17.0、§17.2 第 4 条） |
