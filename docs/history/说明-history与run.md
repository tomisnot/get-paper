# 账本与运行通道的现行设计

这份文档记录当前实现的结论，避免把历史方案继续当作扩展方向。

## 账本

PaperPilot 现在只有一个事实流：SQLite 的 `events` 表。业务表保存当前物化状态，事件表保存发生过的事实；两者由同一数据库事务维护。mecha 的 `History` 是这条事实流的读取、订阅和配置投影门面，不再拥有一份独立的逻辑账本。

每条事件保留以下通用信息：

- 数据库分配的 `seq`，它是唯一顺序；
- `operation_id`，把一次命令、长任务及其产生的多条事件归在一起；
- `role`：`action`（操作及结果）、`mutation`（业务变化）、`derivation`（AI 判断或其它派生事实）；
- `actor`、`reason`、`call_id`、前后像和不透明结果引用。

`History` 在写入前会吸收同一落地点上其它写口已经提交的事件，并采用 Provider 返回的真实序号。因此“命令审计”和“业务事件”不会再因为进程内缓存或各自计数器而分叉。Provider 只需实现 `append/since/last_seq`；如果能返回数据库行，mecha 会使用其中的真实 `seq`。

`fold` 仍然存在，但它只是纯投影函数：

```text
事实流 → 配置快照 / KnowledgeMap / activity 读模型
```

它不写数据库，也不替代业务表。Tutor 的 checkpoint 是可丢弃缓存，GP 的关系表是事务内维护的当前物化状态。项目只需要提供自己的投影函数，领域语义留在项目侧。

GP 以前的“自有事件 + mecha History”已经完成落点收编，但旧的 `History` 缓存分叉问题曾经真实存在；这次修复的是写流和读取门面，而不是继续增加两区互引逻辑。

## command、run 与 job

mecha 的底层统一概念是一次 `operation`：它有一个关联号，事件、结果和错误都挂在这笔操作上。

- `command` 是受写权、作用域和审计治理的公开入口；
- `run` 是框架给计算型项目保留的执行契约；
- `job` 是长时间操作的执行形态。

三个词不需要在框架层强行合并成一个 API，但它们共享同一操作关联和账本语义。

PaperPilot 不再把 `run_pipeline` 投给 AI。AI 面只提供：

```text
fetch → prepare_review → submit_review → finalize_briefing
```

其中判断由外部 DSH/MCP 完成，软件负责提供原料、接收结果、保存产物和记录事实。一键 `paperpilot run` 仍可供人类或调度器使用，它是确定性规则筛选和抽取式摘要，不再在软件内部调用模型；长任务也不再额外维护一套 `submit_job/read_job/cancel_job` 工具。

## 已删除的历史路径

GP 内部的 `UnifiedAIAdapter`、`LLMPort`、`LLMRanker`、`LLMSummarizer` 以及 API key、供应商、模型地址等配置已删除。确定性基线保留在 `infra/ai/baseline.py`，它不调用模型，也不冒充 AI；真正的内容判断只在 DSH/MCP 评审流程中产生。

## 新项目接入 mecha 的最小路径

1. 提供一个 `LogSink`，或把 mecha 事件写入自己的事务数据库；
2. 用 `assemble(...)` 接入 `History/Gate/Surface`；
3. 写操作注册成 `CommandSpec`，业务事件在命令处理过程中写入同一操作范围；
4. 需要特殊当前状态时，只注入一个项目侧投影函数；
5. 需要长时间执行时，把同一操作交给 Job Provider。

框架不要求项目复制 GP 的事件模型、数据库表或一组强制文档。项目只实现自己的事实和投影，治理、关联号、序列和失败形状由 mecha 提供。
