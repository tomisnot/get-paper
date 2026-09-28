# PaperPilot 📄

arXiv 每日文献情报系统：**抓取 → AI 智能筛选 → AI 结构化精读 → 每日简报（本地 Web）**。

想看日报时打开软件，让 AI 把关注领域的新论文筛完、读完、写成简报（脉冲式，无常驻定时）；
同时沉淀一个可全文检索的个人论文库，支持收藏、笔记（BibTeX 导出为规划项）。

> **文档三层**：[`PRINCIPLES.md`](docs/PRINCIPLES.md)（项目宪法 / 基调）→ [`SPEC.md`](docs/SPEC.md)（要实现什么）→ [`DESIGN.md`](docs/DESIGN.md)（怎么实现）；决策冲突时以 `PRINCIPLES.md` 为准。
> 基调一句话：**人机共享的 Web 工作台**——对话驱动 AI + 渐进式工具固化，人和 AI 共同操作同一份内部状态。
> AI 能力统一接入走 **mecha** 框架（已接入，见下节）；本项目只定义契约（`domain/ports/ai.py`），换 AI 后端只改一个 Adapter。

## 它所在的那套东西：四个兄弟项目的关系

本仓不是孤立项目——它是**用 mecha 框架做的三个真实项目**之一：

| 项目 | 是什么 | 用 mecha 的哪一面 |
| --- | --- | --- |
| **mecha** | **框架**（操作者无关层）：Gate / History / Surface / Monitor + Provider 层 | —— |
| **get-paper** | 论文管理与推荐（**本仓**） | **命令面 + 工具面（MCP）** |
| **energy-level** | 物理仿真 | **CodeAct 通道** |
| **ml-toolbox** | 机器学习试验台 | **宿主适配 + 面板** |

⚠ **三个项目是用这套框架做的真实项目，不是框架的一部分**；**框架里没有任何领域实现**
（没有论文、没有物理、没有 ML —— 只有"谁在写、写了什么、怎么看见"这一层）。

> ⭐ **该从哪读起**：先读框架的 **[`docs/接入指南.md`](https://github.com/tomisnot/mecha/blob/main/docs/接入指南.md)**
> ——它讲清"哪些归框架、哪些归你"。
> ⚠ **陌生人最容易犯的错**：把几个示例仓当成框架的目录树，**以为框架里有那些领域东西**。
> 框架里没有；本仓的领域代码全在 `src/paperpilot/`，`src/paperpilot/mecha_adapter/` 才是"接进来的那一段"。

## 怎么装

⚠ 先看清一件事：**统一入口（`paperpilot` / `paperpilot ai`）需要 mecha（框架）**；
**纯 Web 界面（GUI）本体不依赖它**，但**当前版本的 CLI 入口在导入时会用到 mecha** ⇒ 想跑入口就得装上。

```bash
# ① 推荐：含 AI 面所需的框架
python -m venv .venv
.venv\Scripts\pip install -e ".[dev,mecha]"          # Windows
# source .venv/bin/pip install -e ".[dev,mecha]"     # Linux/macOS
```

```bash
# ② 只装本仓本体（不含框架；见上面那条提醒）
.venv\Scripts\pip install -e ".[dev]"
```

**框架的两种来源（mecha 不进 PyPI）**：

- **发布 / 他机**：`[mecha]` extra 走 GitHub 直接 URL，钉 `v0.2.0` tag
  （`mecha[mcp] @ git+https://github.com/tomisnot/mecha@v0.2.0`）。
  ⚠ **如实说明**：该 URL **今天可能还装不上**——实测它现在只能 clone 到**空仓**、**找不到 `v0.2.0` tag**
  （**不是 404**，是仓里还没有那个 tag）⇒ **等上游仓就绪后即可解析**。
- **本机开发**：走 editable，**前提是两仓是同级目录**（框架仓与本仓并排）：
  ```bash
  .venv\Scripts\pip install -e "..\mecha[mcp]"   # 先把框架装成 editable
  .venv\Scripts\pip install -e ".[dev]"          # 再装本仓（`.` 不会去抓那个 URL）
  ```
  ⚠ 别把本机路径写进 `pyproject.toml`（会污染发布物）——那里只放 URL 形式。

Windows 上也可以直接双击 **`开始.bat`**：它建虚拟环境 → 装 Python 依赖 → 装插件依赖 → 启动
**Web 工作台（自动弹浏览器）+ mecha MCP + cockpit + dsh AI 界面**，首次约 1–3 分钟。

## 怎么跑

**两种用法（两个"前门"，都是前台可见）：**

- **人面（你自己用）** = Web 工作台。起服务后**自动弹浏览器**到 `http://127.0.0.1:8080`：
  看今日简报 / 检索论文库 / 标已读收藏笔记 / 下 PDF / 改主题参数 / 跑批 / 切写权模式（`/settings`）。
- **AI 面（对话驱动）** = dsh（复用现成 harness）。在 dsh 里跟 AI 说"看看今天候选、帮我评审生成简报"；
  dsh 右栏另有「📄 简报」（PaperPilot Web 可视化面）与「◈ 监控」（AI 干了什么 = mecha cockpit 原生面板）。

> Web 本质是本地服务（无独立窗口），"前台"就是浏览器；所以启动时会**自动帮你打开**（`--no-open` 可关）。
> "人面写"与"AI 面写"走**同一道写权门**（**默认 `--mode open` = 两侧都能写，人机互不挤**）。
> 要独占或急停，去 Web 的 **`/settings` → 写权模式**卡切换（「放开 / 授予 AI / 取回 human / 锁定」，
> 需控制口令）——**切换只由人类侧发起，且不会被谁偷偷改回去**。

手动方式（等价）：

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev,mecha]"   # Windows（Linux/macOS 用 .venv/bin/pip）

# 离线演示：不联网，用内置样例灌库并生成今日简报
.venv\Scripts\paperpilot demo

# 打开面板（默认 127.0.0.1:8080，内置每天 07:30 自动跑批）
.venv\Scripts\paperpilot
```

**排障**：`paperpilot ai` 起不来 dsh 时——① `cd dsh && npm install`；② 报
`EPERM ~/.dsh/profiles/web/cordis.yml` 说明该 profile 被占用（关掉其它 dsh 实例）；
③ 没装 dsh 就 `npm i -g @deepseek-ai/dsh`（没装也能用：自动降级纯 Web 模式）；
④ 报 `ModuleNotFoundError: mecha` ⇒ 见「怎么装」，`pip install -e ".[dev,mecha]"`。

## 功能一览

| 功能 | 说明 |
| --- | --- |
| 每日简报 | 候选 → 硬规则过滤 → AI 相关性打分 → 主题配额筛选 → AI 结构化精读 → Markdown/Web 简报 |
| 多主题管理 | 每个研究主题独立关键词/分类白名单/排除词/配额/阈值，配置即改即生效 |
| 论文库 | SQLite FTS5 全文检索（标题/摘要/TL;DR），评级/分类/关键词过滤 |
| 阅读态 | 已读/收藏/不感兴趣/笔记 |
| 离线演示 | `paperpilot demo` 用内置样例不联网跑通全链路 |
| 数据自主 | 全部数据在本地 `data/`（SQLite） |

## 日常使用

```bash
paperpilot                 # 统一启动：Web + mecha MCP + cockpit + 每日调度（自动开浏览器到工作台）
paperpilot --no-open       #   同上，但不自动弹浏览器
paperpilot ai              # AI 模式：上面那些后台起 + 前台弹 dsh（AI 在 dsh 里驱动）
paperpilot web             # 只启动 Web（不接 mecha 栈；无 MCP/监控）
paperpilot fetch --days 3  # 抓取近 3 天提交的新论文入库（遵守 arXiv 3s 限速）
paperpilot run             # 立即跑一次「打分→精读→简报」
paperpilot run --force     # 当天已有简报也重跑
paperpilot topics          # 查看当前主题
paperpilot tools           # 列出全部能力（中性能力层，自描述；--json 供程序解析）
paperpilot call <能力> -p k=v  # 调用一个能力，输出统一信封 JSON（外部程序/AI 用）
paperpilot backup          # 打包 data/ 到 backups/
```

Web 页面：`/` 今日简报 · `/digest/{date}` 历史简报 · `/papers` 论文库检索 · `/papers/{arxiv_id}` 详情与笔记 ·
`/activity` **记录仪**（**两区**：上区"数据变更"= 域事件 `before→after` + 一键撤销；下区"操作审计"= mecha 框架账
`command.*`；两区按实体标识互引） · `/settings` 主题与参数管理、手动触发跑批、**写权模式（含控制口令）**。

> ⚠ **`/monitor` 页已退役**：AI 监控改走 **dsh 右栏的原生面板**（共享资产 `dsh-panel/`）——
> **Web 侧不再有第二套审计视图**。**`POST /monitor/mode` 控制端点仍在**（人类控制端点，路径与鉴权未动），
> 它的 UI 落点搬到了 `/settings` 的「写权模式」卡。

## AI 接入：mecha 操作者无关层

**AI 对话、模型管理、界面全部复用 DSH（DeepSeek Harness）**；PaperPilot 经 **mecha** 把中性能力层投影成
AI 面（MCP 工具）+ 监控面（cockpit），人机同过**一道写权门**（authority）：

```
DSH（AI 对话/管理/UI） ──MCP(streamable-http, 仅 localhost)──▶ mecha 投影（.mcp-port 发现）
        │                                                          │ 工具 = 能力层薄封装；写经命令面（authority 门 + 审计）
        └── dsh/ 插件（Cordis）──────────────────────────────────────┘
              · 自愈 MCP 桥：服务重启自动重连重试（治 dsh stock 桥的永久 404 死区）
              · 工具原生注册：mcp__paperpilot__* 供 AI 调用
              · 「📄 简报」按钮：PaperPilot Web（可视化面）挂进 dsh 右栏
              · 「◈ 监控」页签：AI 干了什么（mecha cockpit 的原生面板）
                地址走**同源只读路由** `/paperpilot/monitor-url` ← 读项目根 `.web-port`（Web 真 listen 后才写）
                ⇒ 换端口自愈；**读不到就显可读错误，绝不空白、绝不回落默认端口**
```

> **写权切换要控制口令**（`POST /monitor/mode`）：启动 `serve` / `ai` 时控制台打印，
> 存于**仓外** `~/.paperpilot/control-token`；**fail-closed**——没有口令一律 **403 + 可读错误**，
> 不存在"默认放开"这一档。
> ⚠ **边界（明说，不假装在防）**：这个口令挡的是**本机其他进程**，**挡不住有权读你文件的 AI**
> ——它能读到那个文件。放在仓外只是让"AI 不能自授权"这条边界**尽量**成立，**不是**一道挡住它的墙。

**AI 协作三段流程**（MCP 工具，AI 在 DSH 对话里驱动）：

| 阶段 | 工具 | 做什么 |
| --- | --- | --- |
| 1 | `fetch_papers` → `prepare_review` | 抓取入库 → 取过规则后的候选（含主题画像、摘要截断、基线分） |
| 2 | `submit_review` | AI 逐篇评审：`score/label/reason` + 入选者的结构化 `summary`（只依据给定摘要，不编造） |
| 3 | `finalize_briefing` | 用 AI 评审（缺的用基线分）筛选、精读、生成简报落库 |

**归因与记录仪**（写入全部留痕）：

- 所有写入工具接受 `reason`（为什么）；写入带 `actor`（ai/human）进 **append-only 事件总线**
  （DB 触发器钉死只增不改：任何 UPDATE/DELETE 都被拒绝）；写同时经 mecha 命令面审计进 History（与 dsh call_id 互引）；
- `read_activity(since_seq/actor/op)` 读「谁、何时、为什么、改了什么」（diff-since-seq + 过滤 + 体积闸）；
  Web 侧对应只读页 `/activity`（域数据 + 框架账两区）；
- 写错了 `undo_change(seq=0)` 撤销最近一条可逆操作（按事件的 before 快照回写）；
  抓取入库/简报定稿**不可逆**，undo 会明确拒绝并说明原因，不静默；
- 不可逆操作、未分类异常都带可教学 hint（第一次错就能改对）。

不走对话、程序化使用时：`paperpilot run` 走程序化档——配了 API key 就用
`unified`（OpenAI 兼容，DeepSeek 默认），没配就 `heuristic` 本地 Mock，**任何一档流程都完整**。

## 配置（`config/settings.yaml`）

⚠ **真配置不进仓**（`.gitignore`）：仓里发布的是 **`config/settings.example.yaml`**（默认值/空主题）。
**没有 `settings.yaml` 时程序会回落到它**（它也没有就用代码内默认值）⇒ clone 下来直接能跑；
设置页保存**只写你的 `settings.yaml`**，不会改写发布物。

```yaml
data_dir: data            # 相对路径基于项目根
lookback_days: 7          # 候选论文只看最近 N 天进入库的
arxiv_categories: [cs.CL, cs.AI, cs.LG]

scoring:
  threshold: 0.6          # 入选简报的最低相关性
  quota_per_topic: 4      # 每主题每天入选上限
  max_papers: 12          # 每天简报总上限
  max_per_author: 1       # 同作者每天上限
  must_read_cap: 3        # 必读直通上限

ai:
  provider: auto          # auto | unified | heuristic | off
  api_key: ""             # 建议留空，用环境变量 PAPERPILOT_API_KEY / DEEPSEEK_API_KEY

topics:
  - name: 大模型推理与 Test-Time Compute
    categories: [cs.CL, cs.AI]
    keywords: [reasoning, chain-of-thought, test-time computation]
    exclude_keywords: [survey]
    quota: 4
    threshold: 0.6
```

**topics 以 YAML 为唯一事实源**：Web/CLI 对主题的增删改都会写回 `settings.yaml`，数据库里的 `topics` 表只是打分外键镜像。

## AI 接入（两轨，见 `docs/DESIGN.md`）

**主轨 = DSH/MCP**（`paperpilot ai`，上面那节）：AI 对话、模型管理、UI 全部复用 DSH，
PaperPilot 把中性能力层投影成语义工具（经 mecha 命令/门/审计）。错误可教学
（`{ok:false, error:{kind,hint,suggest}}`）、回程过体积闸（截断必带「截了多少/完整数据去哪看」）、
工具与 CLI/Web 走同一批 service。

**辅轨 = 程序化 LLM**（无对话的程序化使用，如批量脚本）：`infra/ai/unified.py` 的 `UnifiedAIAdapter`
按 OpenAI 兼容协议直连（DeepSeek 默认，换后端只改 `ai.base_url` + `ai.model`）：

- 配了 key（`ai.api_key` 或 `PAPERPILOT_API_KEY`/`DEEPSEEK_API_KEY`）→ `unified` 档；
- 没配 → `auto` 自动落 `heuristic`（本地 Mock：关键词重合度 + 抽取式摘要），不联网也能跑；
- AI 故障时自动降级，简报照常生成并标注原因（`ai_calls` 表记账：耗时/token/成败）；
- 错误分类可教学：401 认证 / 402 余额 / 429 限速（重试）/ 400 上下文超长 / 空 content，
  每条都带「下一步该怎么办」。

## 项目结构

```
src/paperpilot/
├── config.py              # YAML 配置加载（pydantic；缺 settings.yaml 时回落到 example）
├── mecha_adapter/         # mecha 接入：能力→工具、写命令、Gate 配置、cockpit 监控、MCP 端点、作用域策略
├── domain/                # 纯业务：不认识 HTTP/SQLite/arXiv
│   ├── models.py          # AI 边界 DTO（RelevanceScore/PaperSummary/BriefingContent）
│   ├── policy.py          # 硬规则 / 配额 / 兜底打分 / 抽取式摘要（纯函数）
│   ├── pipeline.py        # DailyPipelineService：run() 一键 + prepare/submit/finalize 分段
│   ├── services.py        # RetrievalService：检索与阅读态门面
│   └── ports/             # 契约：ai.py（对外）、repo/render/notify（对内）
├── infra/                 # 外部世界
│   ├── arxiv.py           # arXiv API 客户端：限速/重试/缓存/Atom 解析
│   ├── orm.py / db.py / fts.py / repo.py   # 数据表（含 append-only events）
│   ├── render.py / notify.py
│   └── ai/                # errors（可教学错误）/ heuristic（Mock）/ llm_based / unified（OpenAI 兼容客户端）
├── app/
│   ├── container.py       # DI 绑定（换 AI 实现只改这里）
│   ├── web.py + templates/# FastAPI + Jinja2，无 CDN 依赖（含「记录仪」两区只读页）
│   └── cli.py             # typer CLI（含 ai 模式 launcher；缺框架时给可教学错误）
└── data/sample_arxiv.xml  # demo 样例（离线）
dsh/                       # DSH 插件（Cordis）：自愈 MCP 桥 + 工具注册 +「◈ 监控」原生页签
docs/                      # PRINCIPLES / SPEC / DESIGN / GAPS（技术文档）
tests/                     # 契约 / 解析 / 策略 / 流水线 / Web / mecha 适配 / 归因记录仪
```

## 测试

```bash
.venv\Scripts\python -m pytest        # 全部离线可跑（含真 HTTP + 真 MCP 协议的 e2e）
.venv\Scripts\python -m ruff check .  #  lint
cd dsh && npm test                    # 插件：自愈重连桥 + 端点解析
cd dsh && npm run typecheck           # tsc 零错误
```

覆盖：AI 契约（合法/非法 JSON、长度不符必须抛错）、UnifiedAIAdapter（MockTransport：错误分类/重试/token 记账）、
Atom 解析与去重、规则过滤与配额、流水线幂等与降级、**MCP 分段协作全流程（prepare→submit→finalize，
工具直调 + 真协议活体）**、FTS 检索、Web 路由与配置写回、DSH 插件自愈桥（头号验收：服务重启→旧 session 404→自动恢复）、
**归因与记录仪判据**（写入必带 actor/reason；events 只增不改；undo 往返一致 + 不可逆拒绝）、
**作用域策略真的生效**、**能力 `kind` 与行为对账**（读 / 读+遥测 / 写三档）。

## 已知取舍

- **实体即 SQLAlchemy ORM 模型**（而非纯 pydantic 领域模型）：个人自用项目的务实简化，Port 层保证业务不依赖具体 IO。
- **进程内调度**：APScheduler 适合单机常驻；若进程被杀，用 Windows 任务计划/cron 调 `paperpilot run` 兜底。
- **arXiv 官方 API**（3s 限速）而非爬 HTML：合规且稳定；只抓关注领域，请求量很小。
- **DSH 插件的 client 半**（简报面板）需真 `dsh web` 活体验证；host 半（工具+重连）已本地全验证。
- **编排面（CodeAct）未做**（`docs/GAPS.md`）：当前"流水线即编排"（三段评审协议）；如要 ad-hoc 多步组合再加。
- **CLI 入口依赖框架**：纯 Web 的 GUI 本体不依赖 mecha，但入口模块会 import 它（缺它时给可教学错误）。
- 智能问答、语义检索、BibTeX 导出为规划项（`QAPort` 已预留）。
