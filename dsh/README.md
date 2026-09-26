# paperpilot-dsh —— PaperPilot 的 dsh 集成插件

一个 **out-of-tree** 的 dsh（Cordis）插件，把 PaperPilot 接进 dsh：**接管 MCP 连接生命周期**
（治好 dsh stock HTTP 桥"服务重启即永久 `Session not found`"的死区），并把 PaperPilot 的
语义工具**原生注册**进 dsh；同时在会话头部加一个「📄 简报」开关，把 PaperPilot Web
（今日简报/论文库/设置）挂进 dsh 右栏。

> 架构与纪律详见 `docs/DESIGN.md` §17（对齐 Energy Level `re0-mecha-dsh` 的成熟模式）。

## 它解决什么

dsh 的 MCP 桥只在 `transport.onclose` 时重连；streamable-http 的 POST 失败（服务重启后旧
`Mcp-Session-Id` → 404）**只抛错、不触发 onclose**，客户端永久卡死，只能重启 dsh Host。
本插件用**自写的自愈桥**接管：任何一次调用遇 session 失效/连不上 → 拆旧连接 → 重新
`initialize`（拿**新** session）→ 重试；并有后台退避重连、健康信号、服务未起时不阻塞加载。

## 架构（分层，核心可独立单测）

```
src/index.ts               HOST 入口：apply(ctx,config) → 建桥 + 注册工具 + 状态信号 + 面板地址路由
src/host/mcp-bridge.ts     自愈重连桥（**零 SDK/零 dsh 依赖**，纯逻辑，可注入 fake 单测）
src/host/mcp-session-http.ts  唯一 import @modelcontextprotocol/sdk：每次新建会话（=重连拿新 session）
src/host/register-tools.ts MCP 工具 → ctx.tools.register（照 dsh mcp-client/tools.ts 的两阶段 swap）
src/host/config.ts         端点解析（mcpUrl / .mcp-port / 默认 8780）
src/panel/*                面板共享资产（**逐字复制**自 mecha 参考实现，只有 panel-config.ts 是本项目的值）
src/client/panel-address.ts 取面板地址：同源 fetch 地址路由 → {base}（React 无关，可 Node 直接单测）
src/client/panel-state.ts  面板状态判定：要么可用绝对地址、要么可读错误（**没有"空白"这一态**）
src/client/*               CLIENT 半：📄简报 / ◈监控 开关 + 右栏 iframe + 错误卡 + 面板模式 CSS
types/dsh-shims.d.ts       本地最小类型 shim（@deepseek-ai/* 是宿主提供的 peer，本地装不到）
```

## 面板地址：**同源只读路由**（不回落、现读、读不到就报错）

面板（client 半）**不能自己读磁盘**，而 Web 的端口是**运行期决定**的 ⇒ 地址必须由 host 半
转一次。本插件按 `mecha/mecha/dsh-panel/` 的**共享参考实现**做（[复制约定](src/panel/README.md)
的正文除外逐字一致）：

1. launcher（`paperpilot serve` / `ai` / `web`）在 Web **真的开始 listen 之后**把裸端口写进
   项目根 **`.web-port`**（照 `mecha.portfile` 规则：启动前清陈旧、收尾只删自己的）；
2. host 半注册只读路由 **`/paperpilot/monitor-url`**：读 `.web-port` → 回 `{base}`；
3. client 半**每次打开面板都现取**该路由 ⇒ Web 换端口后下一拍自愈，不需要重启 dsh。

三条纪律（都有判据）：**绝不回落默认端口**（读不到 ⇒ 路由 `503` + 可读错误，失败体**没有**
`base`）；**端口文件名必填**（`panel-config.ts` 的 `PORT_FILE`，库不替项目猜）；**缺地址/缺容器
一律显示可读错误卡**，`panel-state.ts` 里**不存在**"既不是地址也不是错误"的第三态
（迁移前 `readBootstrap()?.webUrl || ''` 会产出空串 ⇒ iframe 永不设 src = 纯白；视图为
`monitor` 时还会退化成相对路径 ⇒ 被浏览器按 dsh 自己的域解析）。

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
| `mcpUrl` | `''` | 显式 MCP 端点；留空则从 `mcpPortFile` 解析 |
| `mcpPortFile` | `.mcp-port` | launcher 写的端口文件（相对 dsh 的 cwd） |
| `serverName` | `paperpilot` | 工具命名空间 → `mcp__paperpilot__<tool>` |
| `projectRoot` | `process.cwd()` | 面板端口文件（`.web-port`）所在的项目根；launcher 已把 dsh 的 cwd 钉在项目根 |
| `mcpToken` | `''` | 远程鉴权 token（→ `Authorization: Bearer`）；本地默认空 |

> 面板地址**没有**配置项：它只能来自 `.web-port` + 同源路由。曾经有个 `webUrl`（默认
> `http://127.0.0.1:8080/`）——与 `settings.yaml` 的 `web.port` **各写一份**，换端口即漂移，
> 且注入缺失时面板静默空白。已删除（2026-09-26 迁移）。

## 本地开发

```sh
cd dsh
npm install --legacy-peer-deps      # @deepseek-ai/* 是 peer（宿主提供），故 legacy-peer-deps
npm run typecheck                   # tsc --noEmit（host + client + panel 副本 + shim）
npm test                            # ① 进程内迷你 runner（自愈桥 + config，14 项）
                                    # ② npm run test:panel ← node --test（面板判据，30 项）
npm run test:panel                  # 只跑面板判据（src/panel/*.test.ts + test/panel.test.ts）
```

> 两套 runner 并存是有意的：自愈桥/config 的测试用 `test/harness.ts` 的零依赖进程内 runner
> （其按文件 spawn 在受限环境会 EPERM）；而**面板判据是共享资产**，随参考实现一起复制、
> 必须保持 `node:test` 原样（`node --test` 在本机实测可用，且那几份测试**零 npm 依赖**——
> 只用 `node:test`/`node:assert`/`node:fs` ⇒ 不需要 `node_modules`）。

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
  **R7 能红证据**（实测，非声称）：把 `panel-state.ts` 的空地址分支改回"相对 src" ⇒
  `node --test` **pass 9 / fail 4**；把 `panel-config.ts` 的 `PORT_FILE` 清空 ⇒ 参数块与
  **Python 侧跨语言守卫**同时红（`test_cli_startup.py::test_web_port_file_name_matches_dsh_panel_config`）。
- 仓内门禁入口：`tests/test_dsh_panel.py`（pytest）会跑上面那三份 `.ts` 判据，并断言
  **零跳过**（全跳过也是 exit 0，只看退出码会假绿）。
- 活体验证（需真 `dsh web`）：插件加载 → `mcp__paperpilot__*` 工具可调；杀服务再起 → 工具
  自动恢复；会话头部「📄 简报 / ◈ 监控」按钮开/关右栏面板。
- **未验证**（明确不宣称）：面板在**浏览器里**的渲染（本机没有可驱动的浏览器）——
  `panel-state.ts` 的判定与错误卡 DOM 只有 Node 级判据，**没有跑过真页面**。
