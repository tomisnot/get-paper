---
name: paperpilot
description: >-
  PaperPilot 论文情报系统的操作纪律（DSH 对话驱动）。当抓取 arXiv、评审候选、
  生成日报或月度合集、刷推荐流（feed）、调主题/配额/画像参数时必读。覆盖三段评审
  SOP、feed 四道召回与刷新自由、行为信号与画像纪律、token 经济姿势、requeue 回炉、
  写权边界与人类专属操作。
---

# PaperPilot 操作纪律（AI 面）

## 核心定位

- AI 面（`mcp__paperpilot__*` 工具，约 34 个）是**主轨**：智能筛选由 DSH 里的 AI 完成；
  软件内**不接 LLM key**（unified/heuristic 只是无人时的兜底档，别主动启用）。
- 所有写入过同一道写权门。发起写入前先 `read_authority`（mode / ai_can_write /
  how_to_open）——被拒了再查是盲撞。
- 读操作不进监控面（那是状态机的事）；你的每次刷流/信号会进域归因总线，
  Web `/activity` 可查——所以**不需要为"留痕"额外发明调用**。

## 一条总纲：显式意图 > 启发式/配置

系里到处是同一个原则的实例，碰到没见过的工具也照此推：
`arxiv_ids` 点名不受 review_floor 拦（N12）；`finalize_briefing(max_items)` 胜
配置篇数；`offset/mix/quotas` 参数胜默认预设；gate 配置（set_config）经命令面全段
生效、但显式调用参数又胜 gate。次序：**用户当场的话 > 你本次调用的参数 > 运行配置
> 出厂默认**；系统被配置拦下时会响亮给路（notes/hint/suggest），静默空转不存在——
遇到就截图报给用户，那是 bug。

## 日常三段评审 SOP（省 token 姿势）

1. `fetch_papers(days=N)`。注意：工具单 query 只回**最新 50 条**；
   **月度/历史回填超出此限**，需操作员用 `ArxivClient` 分页补拉（工具级窗口参数
   尚未支持，别用工具硬扛整月）。
2. `prepare_review(stage=brief)`——只看标题+短摘粗筛（floor 不卡 brief 段）。
3. `prepare_review(stage=full, arxiv_ids=shortlist)`——精评全文；
   **显式点名不受 review_floor 过滤**（点名要的被静默挡回=最坏丢法）。
4. `submit_review(reviews=[...])`——可分批增量提交；每项必含 `arxiv_id/score/label`，
   坏项单独进 `rejected` 不伤全批（缺字段先看 rejected 的 why）。
5. `finalize_briefing(date, force=true)`——定稿。同日重跑**覆盖旧版**（旧版标 superseded，
   一天只认一行）；review 文件按 date 存取，所以 prepare/submit/finalize 必须同 date。

## ⭐ requeue 规则（月报 / 任何汇总前必读）

**凡目标池包含"已被此前评审或定稿消费过的论文"——月度合集、跨期汇总、重审旧池——
`prepare_review` 必须传 `requeue=true`。**

原因：候选只收 `status=new`，而每次 finalize 会把入选标 in_briefing、其余标 archived。
不回炉，这些论文永远不进池——表现为"月报和日报零重叠"。这不是 bug，是"一次性消费"
语义；**操作纪律：聚合前一律回炉**（requeue 只翻近 lookback 内的 archived/in_briefing，
可 undo）。回炉后评审文件会重置，需按原分数重交全部评审（见"打分一致性"）。

## ⭐ 扫雷（rescue sweep）：大池评审后必做

**教训**：brief 只展示基线分前 40 篇，关键词稀疏但标题对口的论文（总览、平台类、
跨域方法）会被静默挡在视野外——实测 184 篇未评里有 12+ 篇比已入选的更相关
（关键词只命中 1-2 个 ⇒ 基线 0.5 甚至 0.12）。**没有任何系统机制自动防这个**，
操作纪律补上：月度/大池评审完成后，对未评论文做一次标题级扫雷。

1. 列出未评候选（操作员只读脚本：读 `data/reviews/{date}.json` 里 `ai=None` 的项，
   join 库表标题；评审文件本身不存标题）；
2. 按标题筛领域相关者，用 `read_paper` 拉摘要；
3. `submit_review` 补评（submit 按全量 stored 校验，展示层之外的也能收）；
4. 再 `finalize_briefing(force=true)`。
纯 AI 面替代：`search_papers` 按主题词分页翻库 + `read_paper` 查打分历史
（model=heuristic 且无 dsh-review 记录 = 未评），略重但可不用脚本。

## 阈值、配额与 caps

- 每主题独立 `threshold/quota`（`update_topic` 自助可调）；`review_floor` 只作用于
  "stage=full 且无点名"的全量评审段。
- `scoring.max_papers` 是全局硬顶：**N15 修复后 `set_config` 的 gate 值直通 finalize**
  （commit `1eafc22`），月度合集可自助抬额、用完还原。注意 gate 值是**运行时态**——
  栈重启后回落 YAML 的 12；要跨重启持久，需人类在 `/settings` 改（写 YAML）。
- **聚合类合集（月报）的真正卡点 per-topic quota**：quota 是给日常注意力设的常数，
  只抬 max_papers 会让新论文把旧 paper 挤出主题槽。月度合集的正确姿势：
  临时抬各主题 quota（放到"阈值即门槛"）+ max_papers ≥ 通过数
  + **`max_per_author` 一并放宽**（同课题组当月多篇后续论文不该被每日单作者上限误伤），
  定稿后**全部还原**日常值。月度篇数应当由"当月通过门槛的全部"决定，不是常数拍脑袋。
- `must_read_cap` 按**次**分配：汇总时当月名额可能已满，同分论文会降 worth——
  分数不变、label 让位是设计，在 reason 里注记即可。

## 打分一致性（跨期汇总时）

- 基线分是纯函数（同篇同主题永远同分）；AI 分是 per-episode 判断。
- 复用时**逐字沿用旧分数**（reason 里注记出处，如"09-26 日报 0.62，分数不变"），
  这是目前保证跨期一致的操作方式。

## 人类专属（AI 不碰，别绕侧门）

- 删主题 / 删简报 / 改主题名：仅 Web `/settings`。AI 工具面无删除是治理设计
  （删除权归人）；`reset_profile`（清画像）同样人类专属——你只读不删。
- SQLite/YAML 直改只在披露过的操作员一次性脚本里出现（用 repo API 并留 actor/reason）。

## 下载与信号（M0 后的形态，本地 PDF 已退役）

- **没有也不需找 download_paper / 本地文件**：站内「⬇ 直下 PDF」/「arXiv 原文」都经
  Web 跳转路由直下，跳转时自动记 `download`/`outbound` 信号（实测）。
- 对话里的口头声明同样算数：用户说“下了/看了/不感兴趣”
  ⇒ `record_signal(download|view|uninterested)`，与实测同表同权；需论文已入库
  （没入库先 `fetch_paper_by_id`）。
- 想给用户一份中文摘要：`fetch_paper_by_id` → 单篇评审（prepare 点名 + submit +
  finalize 小 max_items）或直接 write_note；feed 卡会自动复用这条总结。

## 排障速查

- `prepare_review` 回 `empty_pool`：按 hint 三选一（放宽 lookback / fetch 等新提交 /
  **requeue 回炉**）。
- 候选 >40 看不见全貌：brief 只展示前 `_MAX_REVIEW_CANDIDATES=40` 篇（按基线分排序），
  排后面的靠基线分回落。**总览/视角类论文关键词稀疏、容易被低估**——用户点名或
  `search_papers` 搜到标题后直接补评（submit 按全量 stored 候选校验，展示层之外也能收）。
- 提交被 rejected：列表会点名哪篇缺什么；补齐重提即可（增量合并，不伤已评）。
- 不确定某项能力怎么用：`paperpilot tools` 或 registry `specs()` 有自描述清单。

## 推荐流（feed）：四道召回与刷新自由

- 刷流：`feed_generate(limit=25~40, mix=auto|strict|explorer, days=14)`。用户说“今天想看点野的”⇒ `mix=explorer`；想看多点⇒抬 `limit`。四道=主兴趣/邻接桥/热点作者/探索，**探索 10% 硬地板压不穿**（可顶高）；平时无聊可 `quotas="40,25,10,25"` 这种显式配比微调。
- 每条带 lane 与 why；**播报前 6 条逐条念 why**，探索条说清“这是扩边界位”；why 为空是 bug，举报。
- **刷新决定权全在你**（Web 故意不设刷新按钮）：接着往下端 `offset=`上次回执的 `meta.next_offset`（序列前缀稳定，换屏零重叠零空洞）；换口味改 mix/quotas，排重用 seen_days；`meta.pool_left`小/notes 提“池底”⇒ 先 `fetch_papers` 补货再刷。用户只说话，手段组合你判。
- 画像纪律：`get_profile`（工具名 query_profile）看分类熵与 top 权重；熵<1.0 系统自动加倍探索道（代码保底）；主题只是种子，行为信号才是主画像——想让某人/某类多进快 `record_signal`，想冷却某方向用 `uninterested`（降权不是封杀）。
- 确定性：同参数同画像必同结果——调试时放心重跑；你的痕迹（每次刷流的道组成）在 /activity。
