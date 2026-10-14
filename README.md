# PaperPilot

人机共用的文献研究工作台：收集 arXiv 论文，组织评审与简报，积累可检索的论文库、阅读笔记和引用图谱。

软件保存材料、研究资产与操作记录，并提供确定性的检索和管理能力。AI 在外部宿主中执行评审、总结和研究判断，人通过本地 Web 工作台阅读、反馈与管理；两侧共享数据和写入治理。

## 主要能力

| 能力 | 内容 |
|---|---|
| 收集与简报 | 按研究主题抓取论文、规则过滤、分阶段评审、结构化论文卡和历史简报 |
| 论文库 | 标题、摘要与总结检索；已读、收藏、跳过和笔记 |
| 兴趣推荐 | 由主题和行为信号形成画像，生成带来源理由的推荐流 |
| 引用图谱 | 上下游引用、共引相似、标签、可发布和复用的图视图 |
| 正文精读 | arXiv HTML 章节与文本块、搜索、带原文锚点的批注和阅读记录 |
| 记录与治理 | 操作者、理由、前后状态、可逆操作撤销和人类写权控制 |
| 接入方式 | Web、CLI、自描述能力层与经 mecha 投影的 MCP 工具 |

论文卡、笔记、标签和评审记录在后续工作中持续复用。项目定位见 [设计原则](docs/PRINCIPLES.md)。

## 安装

Python 3.10+。建议同时检出当前 mecha 源码：

```bash
git clone https://github.com/tomisnot/mecha.git
git clone https://github.com/tomisnot/get-paper.git
cd get-paper
python -m venv .venv
```

激活环境：Windows PowerShell 使用 `.venv/Scripts/Activate.ps1`，Linux/macOS 使用 `source .venv/bin/activate`。随后执行：

```bash
python -m pip install -e "../mecha[mcp]"
python -m pip install -e ".[dev]"
```

目录布局不同，请替换 `../mecha`。`.[mecha]` extra 另提供基于框架 `v0.2.0` 的发布依赖；当前开发代码建议与框架源码配套使用。软件入口和受控服务需要 mecha。

## 启动

```bash
# 本地工作台，同时提供 MCP 与只读监控
paperpilot
paperpilot --no-open

# 整站 AI 使用：由 mecha 统一编排软件、dsh 与面板
python -m mecha --all --root .

# 本软件的服务形态，不启动 AI 宿主
paperpilot mcp --with-web
```

Web 默认使用本机地址 `http://127.0.0.1:8080`；以启动输出和实际配置为准。MCP、监控与控制端点通过项目根的端口发现文件接入。

整站 AI 界面需要 PATH 中的 dsh 可执行命令和本仓 `dsh/` 插件资产。插件通过本地 `file:` 引用共享面板；换目录布局时，先将 `dsh/package.json` 的 `@mecha/dsh-panel` 指向所用 mecha 仓的 `mecha/dsh-panel/`，再在 `dsh/` 执行 `npm install` 和 `npm run bundle`。工具聚合与重连由框架负责，项目插件提供研究和监控面板。

## 日常使用

```bash
paperpilot demo                     # 离线样例，会写入当前数据目录
paperpilot fetch --days 3            # 收集近期论文
paperpilot run                      # 程序化生成当天简报
paperpilot run --force               # 生成新的简报版本
paperpilot topics                   # 查看研究主题
paperpilot tools --json             # 查看能力与参数
paperpilot call search_papers -p query=rydberg
paperpilot backup                   # 备份数据
```

主要页面包括推荐流 `/feed`、今日与历史简报、论文库 `/papers`、论文详情、引用图谱 `/network`、操作记录 `/activity` 和设置 `/settings`。

写权默认允许人机共同操作，可在人类设置页或统一启动面板切换。写权变化不要求重启服务；AI 工具面提供查询与业务操作，人类控制面负责切换写权。

## AI 协作流程

AI 可通过 MCP 或能力接口执行研究任务，典型简报流程为：

| 阶段 | 工具 | 用途 |
|---|---|---|
| 收集与准备 | `fetch_papers`、`prepare_review` | 获取候选、主题画像和已有材料；支持先粗筛再精评 |
| 评审 | `submit_review` | 提交逐篇分数、分类、理由与结构化总结 |
| 定稿 | `finalize_briefing` | 按评审和配额生成并保存简报 |

总结应与实际读到的材料对应：只有摘要时按摘要判断，取得正文后再做正文精读。正文读取通过章节和文本块定位，批注保留原文锚点。

研究任务还可使用 `read_paper`、`write_summary`、引用同步、批量标签和图视图工具。参数以 `paperpilot tools --json` 或 AI 宿主提供的 schema 为准；[操作指南](.agents/skills/paperpilot/SKILL.md) 说明典型流程。

读接口提供分页或体积说明；必要的评审候选有专门的返回策略。后台流水线可通过 `submit_job` / `read_job` / `cancel_job` 跟踪，取消是协作式请求。

## 配置与数据

仓库提供 [配置示例](config/settings.example.yaml)。未创建 `config/settings.yaml` 时使用示例或代码默认值；个人配置和密钥不随仓发布。

```yaml
data_dir: data
lookback_days: 7
arxiv_categories: [cs.CL, cs.AI, cs.LG]

topics:
  - name: 大模型推理
    categories: [cs.CL, cs.AI]
    keywords: [reasoning, chain-of-thought]
    exclude_keywords: [survey]
    quota: 4
    threshold: 0.6

ai:
  provider: auto
  api_key: ""
```

主题配置以 YAML 为来源，设置页与 CLI 的修改写回个人配置。论文、阅读态、简报和业务事件保存在本地 SQLite；默认数据目录为 `data/`，可在配置中调整。

当前代码仍保留接入前形成的程序化模型适配路径（`unified` / `heuristic`），主要用于离线演示和历史兼容；它不是新的 AI 接入方向。新的交互式工作应经 mecha 的 MCP 能力面，由外部 AI 操作者完成判断，软件提供原料、接收结果并留痕。程序化路径的退役范围与兼容策略见 [history 与 run 说明](docs/说明-history与run.md)。

业务事件保留操作者、理由及前后状态，并与框架审计关联。可逆操作可用 `undo_change` 撤销；入库、引用同步和简报生成等操作的可逆性以实际接口说明为准。

## 架构

```text
src/paperpilot/
  capabilities/    协议无关的自描述能力与调用入口
  domain/          评审、推荐、检索与业务策略
  infra/           arXiv、SQLite、正文材料与模型适配
  mecha_adapter/   受控命令、工具投影、监控与服务接线
  app/             Web 工作台与 CLI
config/            配置示例
dsh/               AI 宿主中的项目面板
check/ + tests/    结构、业务结果与集成验证
docs/              定位、规格和设计记录
```

业务语义留在 PaperPilot，通用治理与连接使用 [mecha](https://github.com/tomisnot/mecha)。同一框架也用于 [Energy Level](https://github.com/tomisnot/energy-level)、[ML Toolbox](https://github.com/tomisnot/ml-toolbox) 和 Tutor。

## 开发与验证

```bash
python -m pytest
python -m pytest -m integration
python -m pytest -m ""
python -m ruff check .
```

默认执行离线契约和业务结果，真实 HTTP/MCP 与子进程场景单独选择。`check/` 与 `tests/` 都由 pytest 收集，按改动范围运行；不要求工具数量、技能文本或文档章节与手写名单同步。见 [验证说明](check/README.md)。

修改项目面板时，在 `dsh/` 执行 `npm test`，检查 TypeScript 与浏览器构建。

## 当前边界

正文精读目前主要依赖 arXiv HTML，部分论文没有可用正文。引用服务、在线抓取与历史程序化模型路径受外部服务可用性影响。通用研究问答、语义检索和 BibTeX 导出仍是规划方向；已有批注与正文搜索不等于通用问答系统。

## 文档与许可

- [设计原则](docs/PRINCIPLES.md)
- [功能规格](docs/SPEC.md)
- [设计说明](docs/DESIGN.md)
- [AI 操作指南](.agents/skills/paperpilot/SKILL.md)

许可：[MIT](LICENSE)。
