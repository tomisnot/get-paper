# PaperPilot

本地论文台账 + 兴趣画像 + 能力层。**代码记账，AI 出结果，人提问。**

设计见 [docs/设计-总纲.md](docs/设计-总纲.md)——施工以它为准。

它是一台 **mecha 软件**：状态、写权、审计、能力面、监控面由 mecha 承接，
领域（论文、画像、草稿）由本项目实现。

## 依赖

```bash
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\pip install -e "D:\code-nosync\mecha[mcp]"   # 框架（本地仓，含 MCP 传输）
.venv\Scripts\pip install pytest
```

## 起法

**由 mecha 拉起（正常路径）**

```bash
python -m mecha --root "D:\code-nosync\Get Paper"
```

mecha 扫仓根的 `mecha_site.py`，按声明起本软件，然后起它自己的 dsh。
本软件一个进程里同时是四个面：

| 面 | 端点 | 谁用 |
|---|---|---|
| AI 能力面 | MCP streamable HTTP `/mcp`，端口写 `.mcp-port` | dsh |
| 监控面 | cockpit 只读，端口写 `.cockpit-port` | 面板 |
| 控制面 | 写权 / 开手动面，端口写 `.control-port` | 框架、面板 |
| 人面 | Web 工作台 `http://127.0.0.1:8090` | 人 |

**手动起**

```bash
python -m paperpilot --root "D:\code-nosync\Get Paper" serve
```

被 mecha 拉起时，工作台会**自动在浏览器里打开**（`mecha_site.py` 的 argv 带 `--open`）：
等 Web 端口真的在听才开，起不来就不开并出声，不给你一个"无法访问"的标签页。
手动在终端跑默认**不**开（人就在终端里，复制 URL 即可）；想开加 `--open`，不想开加 `--no-open`。
端口被占时**不半启动**：先报错退出，而不是工具面起来了、人面没有。

## 命令行

```bash
python -m paperpilot init                    # 装配一遍，打印库路径/写权/工具数
python -m paperpilot tools                   # AI 能力面清单
python -m paperpilot call read_status -p '{}'   # 与 AI 走同一条门调一个工具
```

## 五个页面

| 页面 | 谁写 | 内容 |
|---|---|---|
| `/` 线性 | AI 的草稿 | 论文条目按顺序排列，每张卡带一句为什么 |
| `/network` 网络 | AI 的草稿 | 条目间的关系图 |
| `/profile` 画像池 | 只读 + 锚点增删 | 三维权重、衰减读数、熵 |
| `/dashboard` 仪表盘 | 只读 | 事件流、库规模、信号漏斗 |
| `/settings` 设置 | 人控 | 草稿管理、系统参数、写权 |

叶子页 `/papers/{arxiv_id}` 摊开一篇论文的全部状态。
**页面上的每个写操作都经命令面**（`stack.invoke_human`），不直写数据库。

## 目录

```
mecha_site.py            站点声明：怎么被 mecha 起起来（数据，不是逻辑）
src/paperpilot/
  config.py              配置（默认值在代码里，JSON 只写覆盖）
  db.py                  SQLite 底座（表、迁移、并发连接）
  ledger.py              事件账（mecha LogSink 的实现，住在同一个库里）
  ids.py                 arXiv 编号归一
  arxiv_client.py        限速 / 重试 / 日历窗 / 翻页到饱和
  scholar.py             引文（Semantic Scholar）
  repo.py                数据访问（一处 SQL）
  scoring.py             特征、权重、衰减、打分、重算
  drafts.py              草稿校验 + 体检
  context.py             领域装配
  mecha_adapter/         mecha 接线层
    config.py              可写配置 schema + 校验 + 种入 Gate
    scope.py               作用域与写权
    queries.py             只读查询（Surface.QUERY）
    commands.py            有副作用的动作（CommandSpec）
    tools.py               AI 能力面（读工具直定义，写工具由命令投影）
    monitor.py             监控摘要 + cockpit
    stack.py               装配点（assemble → 查询 → 命令 → 投影）
    hub.py                 进程入口（四个面）
  web/                   五页 + 叶子页
scripts/import_legacy.py 一次性数据导入
tests/                   69 个测试
```

## 三条硬规则

1. **写必须过门。** 有副作用的动作都是命令，写权检查在任何副作用之前。
2. **草稿里的每一篇都必须能查到、且写得出「为什么」。** 写不进去就是写不进去。
3. **体检只报不拦。** `write_draft` 回执带 `no_card` / `recently_shown` / `top_share`，
   界面上把「无卡」标出来——没读过摘要就推，让它自己露出来。

## 从旧库搬数据

```bash
python scripts/import_legacy.py --source "D:\path\old.sqlite3" --data-dir data
```

搬论文、卡、人的状态、笔记、信号账本、引文边；**不搬权重表**——权重由信号重算，
脚本最后打印新旧画像的 top-10 排序重合度。
