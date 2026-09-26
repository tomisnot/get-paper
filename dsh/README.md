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
src/index.ts               HOST 入口：apply(ctx,config) → 建桥 + 注册工具 + 状态信号 + bootstrap 注入
src/host/mcp-bridge.ts     自愈重连桥（**零 SDK/零 dsh 依赖**，纯逻辑，可注入 fake 单测）
src/host/mcp-session-http.ts  唯一 import @modelcontextprotocol/sdk：每次新建会话（=重连拿新 session）
src/host/register-tools.ts MCP 工具 → ctx.tools.register（照 dsh mcp-client/tools.ts 的两阶段 swap）
src/host/config.ts         端点解析（mcpUrl / .mcp-port / 默认 8780）
src/client/*              CLIENT 半：📄简报 开关 + 右栏 iframe + 面板模式 CSS
types/dsh-shims.d.ts       本地最小类型 shim（@deepseek-ai/* 是宿主提供的 peer，本地装不到）
```

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
最后一层、不落 profile 文件）解决：禁 `remote-web-ui` 与别项目 MCP client
（`mcp-energy-level`/`mcp-re0-mecha`），并逐字段还原 `webserver` 端口表达式。

launcher（`paperpilot ai`）据此：端口自动从 **3081** 选（**3080 永不选**，留给官方 dsh），
依次挂 `cordis.source.patch.yml` + `cordis.isolate.patch.yml` 再传 `--port`。
实测三实例并存：官方 3080 + Energy Level 3081 + 本项目 3082，共享 profile 端口不被回写。

调试开关：

| 命令 | 用途 |
| --- | --- |
| `paperpilot ai --no-open` | 不自动开浏览器，手动 attach 打印出的 URL |
| `paperpilot ai --dsh-port N` | 固定 dsh 端口（默认自动从 3081 选） |
| `paperpilot ai --debug-home` | 用项目内 `.dsh-debug/` 作 `DSH_HOME`：会话/设置/凭据与共享 `~/.dsh` 完全隔离（首次需重新登录） |
| `paperpilot dsh-config` | 只读打印 dsh 组合后配置（验证 overlay：webserver 带 `!!js` 表达式、三插件 `disabled: true`） |

dsh 升级后先跑 `paperpilot dsh-config` 确认 `webserver` 表达式行未变，再信任 `--port`。

配置（在 patch/profile 里给该 entry 的 `config`）：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `mcpUrl` | `''` | 显式 MCP 端点；留空则从 `mcpPortFile` 解析 |
| `mcpPortFile` | `.mcp-port` | launcher 写的端口文件（相对 dsh 的 cwd） |
| `serverName` | `paperpilot` | 工具命名空间 → `mcp__paperpilot__<tool>` |
| `webUrl` | `http://127.0.0.1:8080/` | 右栏面板 iframe 的 PaperPilot Web 地址 |
| `mcpToken` | `''` | 远程鉴权 token（→ `Authorization: Bearer`）；本地默认空 |

## 本地开发

```sh
cd dsh
npm install --legacy-peer-deps      # @deepseek-ai/* 是 peer（宿主提供），故 legacy-peer-deps
npm run typecheck                   # tsc --noEmit（host + client + shim）
npm test                            # 进程内迷你 runner：自愈桥 7 项 + config 6 项
```

> 测试用 `test/harness.ts` 的零依赖进程内 runner（不用 `node --test`：其按文件 spawn 在
> 受限环境会 EPERM；本插件测试全是纯逻辑 + `node:assert`，进程内足够）。

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

- 本地确定性验证：`npm run typecheck`（tsc 零错）、`npm test`（13 项：自愈桥 7 + config 6）。
- 活体验证（需真 `dsh web`）：插件加载 → `mcp__paperpilot__*` 工具可调；杀服务再起 → 工具
  自动恢复；会话头部「📄 简报」按钮开/关右栏面板。
