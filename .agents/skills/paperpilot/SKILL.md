---
name: paperpilot
description: >-
  PaperPilot 论文情报系统的操作纪律（DSH 对话驱动）。当抓取 arXiv、评审候选、
  生成日报或月度合集、刷推荐流（feed）、查引文脉络/文献计量、做资产盘点与趋势统计、
  调主题/配额/画像参数时必读。42 个工具全部入册；AI 面唯一禁忌是 reset_profile。
---

# PaperPilot 操作纪律（AI 面）

> 本文件与工具注册表由判据强制同步（tests/test_skill_sync.py：42 工具漏一个就红）。
> 改工具的人必须同时改这里，否则 CI 不答应。

## 核心定位与一条总纲

- AI 面（`mcp__paperpilot__*`，42 工具）是主轨；软件内不接 LLM key（heuristic 是无人兜底档）。
- 发起写入前先 `read_authority` 看写权开闸（被拒再查=盲撞）；长活走 `submit_job`/`read_job`/`cancel_job`。
- 读操作不进监控（状态机只显变化）；你的刷流/信号进 `/activity` 记录仪——不必为留痕发明调用。
- **总纲：显式意图 > 启发式/配置**。次序：用户当场的话 > 本次调用参数 > 运行配置 > 出厂默认。
  典型实例：`arxiv_ids` 点名不受 review_floor（N12）；`max_items` 胜配置；`offset/mix` 胜默认；
  gate 值（`set_config`）直通命令面全段但被显式参数再覆盖。系统拦截必响亮给路（notes/hint/suggest），
  静默空转=bug，截图报用户。

## 工具全表（42，按场景组；名字以反引号标注=真实工具名）

**读·认知**：`read_paper` 详情+总结+打分史+笔记 | `search_papers` 库内检索(FTS5,offset 分页) |
`read_digest` 简报全文/纯统计 | `query_briefings` 历史简报清单(管理面) | `query_topics` 主题含
authors | `read_config` / `review_status` 评审进度 | `read_activity` 事件+运行+AI 成本 |
`query_profile` 画像 top 权重+分类熵 | `read_authority` 写权现状。

**引文与计量**（S2 实时，缓存过）：`paper_metrics` 一篇的影响力度量 | `read_references` 向前追溯
（按被引排序=奠基候选，带 intents）| `read_citations` 向后看扩散 | `sync_citations` 把引用边落本地
图谱（幂等、可撤）| `sync_cited_by` 反查“谁引了它”入图（下游独立成层，幂等不可撤）|
`tag_paper` 钉六色图论标签（平台源头/理论源头/综述枢纽/实验谱系/下游扩散/动机，可撤）|
`upstream_clusters` 库内多篇同引=思想源头 | `related_papers` 共引相似。

**盘点与统计**：`coverage_report` 有卡/读过/收藏+缺卡工单 | `stats_timeseries` 每日入库/信号漏斗/
简报节奏/token 按用途 | `watch_authors` 作者雷达（主题 authors ∪ 画像作者，只读；入库逐篇
`fetch_paper_by_id`，顺带 `record_signal` 喂画像）。

**获取**：`fetch_papers` 按主题抓近 N 天（3s 限速）| `fetch_paper_by_id` 点名入库（幂等）。

**评审三段**：`prepare_review`（两阶段：stage=brief 短摘粗筛→stage=full+arxiv_ids 点名精评，
点名破 floor；池空可 requeue）→ `submit_review`（可增量；坏项进 rejected 不伤全批）→
`finalize_briefing`（force 重跑；**max_items 当次定篇数**）。一键兜底：`run_pipeline`。

**feed**：见下节专章。

**写·轻操作**（均留痕可逆）：`mark_read` / `star_paper`（逗号多篇、per-item 坏项不伤其余）|
`skip_paper`（同类过滤旧机制，与画像 uninterested 是两码事）| `add_note` 笔记（笔记≠卡）|
`write_summary` **单篇补日报级卡**（总结五段+score/label 成对；feed 卡与详情页自动复用）|
`delete_briefing` 删某天简报（快照留痕可 undo；删主题/改名才是人类专属）。

**主题管理**：`add_topic` / `update_topic`（省略=不动、列表替换语义；含 authors）/
`set_topic_enabled`。**删主题、改主题名：AI 无门，归人。**

**配置与治理**：`set_config`（gate 值，命令面有操作审计；重启回 YAML）| `read_config` |
`read_authority`（写权现状）| `undo_change`（seq=0 撤最近可逆）|
`submit_job` / `read_job` / `cancel_job`（长活；对已完成的取消会如实报"已完成"）。
注意：**开写权没有 AI 工具**（模式切换只活在人类侧 /settings 口令里，这是设计不是缺位）——
被 authority_locked 拒了就 `read_authority` 看现状、请人类开闸，别找后门。

## 日常三段评审 SOP（省 token 姿势）

1. `fetch_papers(days=N)`（单 query 上限 50；月级回填别硬扛，分页或让操作员脚本）。
2. `prepare_review(stage=brief)` 粗筛 → 选 ≤15 篇 → `prepare_review(stage=full, arxiv_ids=…)`。
3. `submit_review` 全量或增量提交；**大池评完做扫雷**：brief 只展示前 40，关键词稀疏但标题对口的
   总览/平台类会被基线分挡在视野外——读 reviews 文件里 ai=None 的项按标题补评（submit 按全量校验）。
4. `finalize_briefing(force=true)`；月度合集见下。

**requeue 铁律**：凡目标池含"被此前评审/定稿消费过的论文"（月报、跨期汇总）⇒ `requeue=true`
回炉（否则池子永远不进=一次性消费语义）；回炉后评审文件重置需重交全量。
**月报姿势**：临时抬各主题 quota + max_papers + max_per_author（定稿后全部还原）；must_read_cap
按次分配，让位时分数不变、reason 注记。**打分一致性**：复用旧分数逐字沿用+注记出处。

## feed 推荐流：面板=期票，驱动权全在你

- 面板只读最新一期（快照、无刷新按钮）；发期=`publish_feed`（参数全显式），预览/调试才用
  `feed_generate`（kind=read_telemetry：读+顺手记账，回执带 lane/why/`meta.next_offset`）。
- 四道=主兴趣/邻接桥/热点作者/探索；**探索 10% 硬地板压不穿**；换页 offset 续 `meta.next_offset`
  （前缀稳定零重叠）；口味"野一点"⇒mix=explorer；池底⇒先 `fetch_papers`。发错期 undo 可回。
- 播报前 6 条逐条念 why（why 为空=bug 举报）；反馈随手 `record_signal`（download/view/
  uninterested，与站内实测同权）。
- 画像：主题只是种子；`query_profile` 看熵；熵<1.0 系统自动加倍探索道（你可再抬）；
  **`reset_profile` 是 AI 面唯一禁忌**（清画像=人类按钮，你只读不删）。

## 下载与信号（本地 PDF 已退役）

站内"直下/原文"走跳转路由自动记 download/outbound 信号；口头声明 `record_signal` 同表同权
（需已入库）。想要某篇的中文摘要：`write_summary` 一张卡，别拉评审三段陪跑。

## 排障速查

empty_pool⇒hint 三选一（lookback/fetch/requeue）· rejected⇒点名缺什么补齐增量重提 ·
authority_locked⇒`read_authority` 看开闸 · 不确定用法⇒`paperpilot tools` 或 specs() 自描述。

## Web 配套面（你干的活在哪被看见）

- **/network 引文网络 = AI 调研成果的显示器**：你用 `sync_citations`/`sync_cited_by` 落边、
  `tag_paper` 钉色，用户回这看图。**单根聚焦 ?root=&depth= 是你该主动用的玩法**：用户说
  “以某篇为中心看图”→ 先 sync 它再给链接（根→它引的→共引上游；反查后下游成层）。
  图例着色靠 `tag_paper`（color_by=tag 在 /settings 切）。节点是站内句柄：
  点击=未入库先 `fetch_paper_by_id` 入库再进管理页；图上永不外跳。**调查完吱声**：“图谱已更新，去 /network 看”。
- **/feed 面板**只读你 `publish_feed` 发的最新一期，无刷新按钮——换页/口味全在你手里。
- **/lab 仪表盘 + /activity 记录仪**：覆盖率/趋势/成本与行为审计的展示面；报数与它同口径，
  同一数字两处真相会被判据拒绝。
- 分工纪律：**写操作永远走命令面**——Web 只有展示与轻操作，没有与你对等的图谱编辑按钮；
  别把“页面没按钮”当“需要人类口述代办”，也别替人类造同款按钮。
