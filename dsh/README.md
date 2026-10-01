# paperpilot-dsh —— PaperPilot 的 dsh 集成插件

一个 **out-of-tree** 的 dsh（Cordis）插件，把 PaperPilot 接进 dsh：**接管 MCP 连接生命周期**
（治好 dsh stock HTTP 桥"服务重启即永久 `Session not found`"的死区），并把 PaperPilot 的
语义工具**原生注册**进 dsh；同时在会话头部加一个「📄 简报」开关，把 PaperPilot Web
（今日简报/论文库/设置）挂进 dsh 右栏。

> 架构与纪律详见 `docs/DESIGN.md` §17（对齐 Energy Level `re0-mecha-dsh` 的成熟模式）。

## 它解决什么

dsh 的 MCP 桥只在 `transport.onclose` 时重连；streamable-http 的 POST 失败（服务重启后旧
`Mcp-Session-Id` → 404）**只抛错、不触发 onclose**，客户端永久卡死，只能重启 dsh Host。
本插件用**资产包的自愈桥**接管（`@mecha/dsh-panel/mcp-bridge.ts`，`file:` junction 依赖；
2026-10-01 起不再是本仓自写、也不再是副本）：任何一次调用遇 session 失效/连不上 → 拆旧连接 →
重新 `initialize`（拿**新** session）→ 重试；并有后台退避重连、健康信号、服务未起时不阻塞加载。

## 架构（分层，核心可独立单测）

```
src/index.ts               HOST 入口：apply(ctx,config) → 建桥 + 注册工具 + 状态信号 + 面板地址路由
src/gp-params.ts           **GP 的参数家**（桥参数 + 面板参数 GP_PANEL；浏览器安全）
src/host/gp-hub.ts         node 侧参数接线：把上面的值填进资产包（本目录唯一保留的 host 文件）
src/client/*               CLIENT 半：◈监控 按钮 + 右栏页签注册 + ☀评审今日按钮（📄简报 iframe 链已退役）
types/dsh-shims.d.ts       本地最小类型 shim（@deepseek-ai/* 是宿主提供的 peer，本地装不到）
```

> ⭐ **面板/桥全部来自依赖包 `@mecha/dsh-panel`**（`npm install file:<mecha 仓>/mecha/dsh-panel`
> ⇒ `node_modules/@mecha/dsh-panel` 是 **Junction**，真·单一副本）。
> **本仓不再持有任何资产副本** ⇒ "副本有没有漂"这个问题**结构上不存在**（改资产 = 改 mecha 仓），
> 故那条**逐字指纹判据已删**（`tests/test_dsh_panel.py`）。
> ⚠ 运行期两条实测坑（都在 npm scripts / tsconfig 里处理了）：
> ① Node 解析 junction 走真实路径 ⇒ 包声明的 optional peer `@modelcontextprotocol/sdk`
> 在消费者侧解析不到 ⇒ 加 `--preserve-symlinks`（TS 侧对应 `preserveSymlinks: true`）；
> ② Node 原生剥类型**拒绝 `node_modules` 下的 `.ts`** ⇒ 跑 TS 一律经 `--import tsx`。
> ⚠ 打包：`tsdown` 默认**外置 dependencies** ⇒ 必须 `deps.alwaysBundle` 把资产**打进** bundle
> （否则产物里留 `.ts` import，走构建产物那条交付路径运行期必炸）。

> ## ⭐「◈ 监控」= **共享资产的原生页签**（2026-09-26 抄装，用户裁决"几乎完全复用 EL，布局也是"）
>
> 监控面板**不是本插件的 iframe 视图**：它由 `src/panel/` 的共享资产提供——
> 数据层 `panel-data.ts`、呈现 `panel-view.ts`（含 `TAB_CSS` = **布局本体**）、壳
> `MonitorTabBody.tsx`（唯一需要 react 的地方，3s 轮询 + `dangerouslySetInnerHTML`）。
> 本项目只提供**参数块**（`ROUTE_PATH` / `PORT_FILE` / `TITLE`）。注册形状抄 EL：
> `sidebarRightTabs.register({id, kind, title})` + `sidebar.right.pane.tab` slot，
> `inject` 加 `sidebarRight` / `sidebarRightTabs`（**顶层**，与 EL 同形）。
>
> ⚠ **两个面板共用右栏 ⇒ 互斥**（有意行为，不是 bug）：◈监控是 **dsh 原生页签**（布局由 dsh
> 管），📄简报是 **iframe + CSS 重排三列**（本项目自有）⇒ 同时开会打架。规则：
> `openCockpit` 先 `setPanelMode(false)`；打开简报先 `sidebarRight.closeTab(...)`。
>
> ⚠ **`TABS` 刻意不填**（页签用共享默认「飞行记录仪 | 配置态」= 与 EL 一致）：资产自测
> `panel-data.test.ts` 的 `tabLabel` 用例**把中性默认写死**却读项目参数块 ⇒ **一填 `TABS` 共享自测必红**
> （已报资产侧，待其"期望值从参数块派生"后再填 PaperPilot 自己的文案）。
>
> ⚠ **当前 `npm run typecheck` 是红的，红在资产**：`panel-data.ts` 用了 `Ev` / `ConfigWire`
> （定义在 `panel-view.ts`）却**没 import** ⇒ 4 条 `TS2304`。资产自测只做**类型剥离**、不做类型
> 检查 ⇒ mecha 自己的门禁看不见；**消费者的 `npm run typecheck` 就是这条契约的验收面**（已报，
> 等上游加 `import type { ConfigWire, Ev } from './panel-view.ts'` 后重抄+复绿；**本仓不就地打补丁**：
> 那会破坏"除参数块外逐字一致"）。
>
> **浏览器安全**：资产已把 `node:*` 边界**结构化**（`routes.ts` 浏览器安全 / 只有 `monitor-url.ts`
> 碰 `node:fs`），并有 import 闭包守卫；**本项目再加一条**——从**本项目**的 `client/index.ts`
> 出发走一遍闭包，断言零 `node:` 且裸包只许 `react`（此前"没撞上 EL 那个事故"只是树摇的运气）。

## 面板地址：**同源只读路由**（不回落、现读、读不到就报错）

面板（client 半）**不能自己读磁盘**，而 Web 的端口是**运行期决定**的 ⇒ 地址必须由 host 半
转一次。本插件按 `mecha/mecha/dsh-panel/` 的**共享参考实现**做（除 `panel-config.ts` 外逐字一致；
⚠ **本仓不保留副本 `README.md`**——权威在框架侧那份，见 `mecha/dsh-panel/README.md`）：

1. launcher（`paperpilot serve` / `ai` / `web`）在 Web **真的开始 listen 之后**把裸端口写进
   项目根 **`.web-port`**（照 `mecha.portfile` 规则：启动前清陈旧、收尾只删自己的）；
2. host 半注册只读路由 **`/paperpilot/monitor-url`**：读 `.web-port` → 回 `{base}`；
3. client 半**每次打开面板都现取**该路由 ⇒ Web 换端口后下一拍自愈，不需要重启 dsh。

三条纪律（都有判据）：**绝不回落默认端口**（读不到 ⇒ 路由 `503` + 可读错误，失败体**没有**
`base`）；**端口文件名必填**（`panel-config.ts` 的 `PORT_FILE`，库不替项目猜）；**缺地址/缺容器
一律显示可读错误卡**，`〔已随简报 iframe 链退役，2026-09-26 第 5 批〕panel-state.ts` 里**不存在**"既不是地址也不是错误"的第三态
（迁移前 `readBootstrap()?.webUrl || ''` 会产出空串 ⇒ iframe 永不设 src = 纯白；视图为
`monitor` 时还会退化成相对路径 ⇒ 被浏览器按 dsh 自己的域解析）。

**资产管不到的那一半（项目侧义务，本仓已兜住）**：资产 README 明说它"只做地址+取数+状态，
不含 DOM / 渲染 / 挂载"⇒ 两条义务归项目：① **挂载失败必须可见**（找不到 `[data-rightbar-col]`
时贴可读错误卡，**禁止静默 `return`**）；② **两跳（iframe）必须可诊断**（外层读不到跨源内层
的 DOM ⇒ 挂之前 `probeReachable` 探 `/healthz`，不让"外层 200、内层空白"成为静默态）。
另外照该 README 的「**抄完照 R17 自查一遍**」，本仓删掉了原先恒真的
`shouldRetry(render)`（调用点那个 `!shouldRetry(...)` 永不成立 = 死缝）；
每个注入点都配了「**换掉它、结果就变**」的用例（`fetchMonitorBase` 的 `doFetch`、
`probeReachable` 的 `doFetch` 都有）。

**安全红线**：插件**只 connect、绝不 spawn 服务**（人启动 launcher = 权威）。换 harness 只丢
本插件，PaperPilot 的独立 MCP server 照用（跨 harness）。

## 加载

```sh
# 首次/新检出必先装依赖（host 代码 import @modelcontextprotocol/sdk + ws）
cd dsh && npm install && cd ..

# 开发期（tsx 直接加载源、无需构建）
dsh web --patch ./dsh/cordis.source.patch.yml

# 交付期（先构建）
cd dsh && npm run bundle && cd ..
dsh web --patch ./dsh/cordis.patch.yml
```

或者直接用 launcher（自动起服务 + 装依赖 + 前台跑 dsh）：

```sh
paperpilot ai
```

## 并存隔离与调试（不干扰其他项目的 dsh）

原理见 mecha-sdk《DSH-实例并存原理.md》：dsh 无单实例锁，硬单例只有**监听端口**，而端口由
**补丁组合链最后一层**决定；共享 profile 里 `remote-web-ui` 的托管块会把 `webserver` 整块
config 换成字面量（`--port` 静默失效）且每次启动把实际端口**回写**共享 profile（last-writer-wins）。
本项目用**进程级隔离 overlay** [cordis.isolate.patch.yml](cordis.isolate.patch.yml)（`--patch`
最后一层、不落 profile 文件）解决：禁 `remote-web-ui`（**本实例的**威胁：钉端口 + 回写共享
profile）、并逐字段还原 `webserver` 端口表达式。

⚠ **overlay 只写本实例需要的隔离，不列举别项目的插件 id**：那属**跨项目知识**——别家改名/
退役，本仓的列举就**静默失效**（实测：曾列举 `mcp-energy-level`/`mcp-re0-mecha`，共享 profile
清掉这两个 id 后只剩 dsh 的 `patch: entry … not found` 警告）。别家的隔离由**别家自己的
overlay** 负责；"同一 profile 混进别家 mcp client" 该由**共享 profile 清**。

launcher（`paperpilot ai`）据此：端口自动从 **3081** 选（**3080 永不选**，留给官方 dsh），
依次挂 `cordis.source.patch.yml` + `cordis.isolate.patch.yml` 再传 `--port`。
实测三实例并存：官方 3080 + Energy Level 3081 + 本项目 3082，共享 profile 端口不被回写。

调试开关：

| 命令 | 用途 |
| --- | --- |
| `paperpilot ai --no-open` | 不自动开浏览器，手动 attach 打印出的 URL |
| `paperpilot ai --dsh-port N` | 固定 dsh 端口（默认自动从 3081 选） |
| `paperpilot ai --debug-home` | 用项目内 `.dsh-debug/` 作 `DSH_HOME`：会话/设置/凭据与共享 `~/.dsh` 完全隔离（首次需重新登录） |
| `paperpilot dsh-config` | 只读打印 dsh 组合后配置（验证 overlay：`paperpilot` 条目在列、webserver 带 `!!js` 表达式、`remote-web-ui` 带 `disabled: true`） |

dsh 升级后先跑 `paperpilot dsh-config` 确认 `webserver` 表达式行未变，再信任 `--port`。

配置（在 patch/profile 里给该 entry 的 `config`）：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `hubUrl` | `''` | 显式 MCP 端点；留空则从 `mcpPortFile` 解析 |
| `mcpPortFile` | `.mcp-port` | launcher 写的端口文件（相对 dsh 的 cwd） |
| `serverName` | `paperpilot` | 工具命名空间 → `mcp__paperpilot__<tool>` |
| `projectRoot` | `process.cwd()` | 面板端口文件（`.web-port`）所在的项目根；launcher 已把 dsh 的 cwd 钉在项目根 |
| `hubToken` | `''` | 远程鉴权 token（→ `Authorization: Bearer`）；本地默认空 |
| `hubHeaders` | `{}` | 额外请求头 |

> 面板地址**没有**配置项：它只能来自 `.web-port` + 同源路由。曾经有个 `webUrl`（默认
> `http://127.0.0.1:8080/`）——与 `settings.yaml` 的 `web.port` **各写一份**，换端口即漂移，
> 且注入缺失时面板静默空白。已删除（2026-09-26 迁移）。

## 本地开发

```sh
cd dsh
npm install                         # 含 @mecha/dsh-panel（file: junction）
npm run typecheck                   # tsc --noEmit（0 错；preserveSymlinks 已开）
npm test                            # ① 进程内迷你 runner（本项目侧）② npm run test:panel
npm run test:panel                  # 面板判据（只剩 test/panel.test.ts；资产自带单测随包走）
npm run bundle                      # 产物：lib/index.mjs + lib/client.js（资产已内联）
```

> ⚠ **跑 TS 一律经 `--import tsx`**：Node 原生剥类型**拒绝 `node_modules` 下的 `.ts`**
> （资产包经 junction 就在这里）——`--preserve-symlinks` 又是 peer 解析的必要条件。
> 两套 runner 并存是有意的：桥/config 的测试用 `test/harness.ts` 的零依赖进程内 runner
> （其按文件 spawn 在受限环境会 EPERM）；面板判据用 `node:test`。
> ⚠ **资产自带的共享单测不在本仓跑**（随包走了；执行面在框架的
> `mecha/checks/dsh_panel_selfcheck.py`，已挂在 `checks/run_all.py`，硬依赖 `node`）。

## 已知约束（从 re0-mecha-dsh 的踩坑记录移植）

- **host = Node 原生 strip-types（strip-only）**：只擦类型、不生成代码。TS **参数属性**
  （`constructor(private x)`）/`enum`/`namespace` 会 `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX` 使
  整棵插件树加载失败 → 显式字段+构造器赋值；tsconfig 已开 `erasableSyntaxOnly` 本地拦。
- **client = dsh `__ModuleLoader__` classic-script 合并包**：每个 client.js 必须自注册
  `window.__ModuleLoader__.load({id, factory})`。普通 ESM 在合并包里是 SyntaxError → 毒化整包
  → tsdown 用 `format:'cjs'` + banner/intro/footer 复刻；`react` 必须 external 经注入的
  require 共享（双 React 会崩）。
- **运行版 dsh 的 layout API 是 `openRightbar/closeRightbar`**（非新源码的
  `openDetails/closeDetails`），误调会打崩整站 → 全部可选链降级 + shim 对齐。

## 验证边界（诚实交代）

- 本地确定性验证：`npm run typecheck`（tsc 零错）、`npm test`（14 项进程内 + 30 项 `node --test`）。
  面板那 30 项里，**本项目自己的**判据（`test/panel.test.ts`，13 项）钉三件事：
  **地址不回落**（缺/坏端口文件 ⇒ `503` 且失败体无 `base`，且不许回落到历史默认 8080）、
  **面板不空白**（R8：`ready`⇒非空绝对地址 / `error`⇒非空可读文案，两分支都被走到且成功态
  真的带上喂进去的地址）、**缺地址不许指向相对路径**（迁移前的 `'' + 'monitor'` 病）。
  **R7 能红证据**（实测，非声称）：把 `〔已随简报 iframe 链退役，2026-09-26 第 5 批〕panel-state.ts` 的空地址分支改回"相对 src" ⇒
  `node --test` **pass 9 / fail 4**；把 `panel-config.ts` 的 `PORT_FILE` 清空 ⇒ 参数块与
  **Python 侧跨语言守卫**同时红（`test_cli_startup.py::test_web_port_file_name_matches_dsh_panel_config`）。
- 仓内门禁入口：`tests/test_dsh_panel.py`（pytest）会跑上面那三份 `.ts` 判据，并断言
  **零跳过**（全跳过也是 exit 0，只看退出码会假绿）。
- 活体验证（需真 `dsh web`）：插件加载 → `mcp__paperpilot__*` 工具可调；杀服务再起 → 工具
  自动恢复；会话头部「📄 简报 / ◈ 监控」按钮开/关右栏面板。
- **未验证**（明确不宣称）：面板在**浏览器里**的渲染（本机没有可驱动的浏览器）——
  `〔已随简报 iframe 链退役，2026-09-26 第 5 批〕panel-state.ts` 的判定与错误卡 DOM 只有 Node 级判据，**没有跑过真页面**。
