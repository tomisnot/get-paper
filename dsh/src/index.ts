/**
 * paperpilot dsh 插件 —— **HOST 入口**（Cordis 插件，函数形态）。
 *
 * ⚠ **2026-10-01：通用机制已全部上提**——本文件只剩**参数与 GP 自己的知识**：
 * 建桥 / 会话工厂 / logger 转发 / `onTools→syncTools` 两阶段 swap / `onStatus` 三态日志 /
 * 监控地址路由的注册与注销 / 收尾顺序 / 端点每次现解的包装 / **SDK 懒加载**，
 * 现在都在资产包的 `mountHostPlugin`（`@mecha/dsh-panel/mount-host-plugin.ts`）里。
 * 面板与桥本身同样来自该包（`file:` junction，单一副本在 mecha 仓）。
 *
 * 留给 GP 的只有三样：**参数值**（`gp-params.ts`）、**端点怎么算**（`gpResolveHubUrl`，
 * 显式 URL > 端口文件 > 项目默认是项目知识）、以及 `paperpilot mcp` 那种**没有 web 服务
 * 也要照常工作**的激活语义（⇒ `hostPluginInject({needsWebServer: false})`）。
 *
 * 安全红线：**只 connect、绝不 spawn 服务**（人启动 launcher = 权威）。换 harness 只丢
 * 本插件，PaperPilot 的独立 MCP server 照用（跨 harness）。
 */
import type { Context } from '@deepseek-ai/cordis'
import { buildRequestInit } from '@mecha/dsh-panel/config.ts'
import { hostPluginInject, mountHostPlugin } from '@mecha/dsh-panel/mount-host-plugin.ts'
import { gpResolveHubUrl } from './host/gp-hub.ts'
import { GP_BRIDGE, GP_PANEL, GP_TOOLS } from './gp-params.ts'

/** Cordis 插件显示名（诊断用；工具命名空间默认同此）。 */
export const name = 'paperpilot'

/**
 * 顶层 `inject`（cordis 要求它在模块顶层）。
 *
 * ⚠ **`needsWebServer: false` 是刻意的**：GP 有 **headless** 用法（`paperpilot mcp` 只起 MCP，
 * 不进 dsh；换 harness / 无 web 服务时桥仍要活）。骨架那句 `ctx.inject(['webServer'])`
 * 在没有 webServer 时**跳过路由**而不是不激活 ⇒ 顶层不声明它，才保住"没有 web 服务 ≠ AI 工具也没了"。
 * （骨架默认给 `['tools','webServer']`；那条会把面板缺失升级成插件不激活。）
 */
export const inject = hostPluginInject({ needsWebServer: false })

/**
 * 插件配置（用户在 cordis.patch.yml 的 entry `config:` 里给；全部可选，缺省在 apply 里兜）。
 *
 * 刻意**不导出 schemastery 的 Config 校验 schema**：那会引入对 `@deepseek-ai/schemastery` 的
 * **运行时** import，而 out-of-tree 插件的 node_modules 里没有它（它是 dsh 宿主提供的 peer）。
 * 去掉后，host 半对 @deepseek-ai/* **只剩类型 import（编译期擦除）**、运行时零 peer 依赖。
 */
export interface Config {
  /** 显式 PaperPilot MCP 端点；留空则从 mcpPortFile 解析。 */
  hubUrl?: string
  /** 端口文件路径（launcher 写；默认项目根 `.mcp-port`，相对 dsh 的 cwd）。 */
  mcpPortFile?: string
  /** 工具命名空间：`mcp__<serverName>__<tool>`。 */
  serverName?: string
  /** 项目根（面板端口文件所在目录；默认 dsh 进程的 cwd = launcher 已切到项目根）。 */
  projectRoot?: string
  /** 远程 MCP 鉴权 token（→ Authorization: Bearer）；本地默认空。 */
  hubToken?: string
  /** 额外请求头。 */
  hubHeaders?: Record<string, string>
  /** 重连退避：首次延迟 ms（不传 = 用桥自己的默认 300）。 */
  reconnectInitialDelayMs?: number
  /** 重连退避：上限 ms（不传 = 用桥自己的默认 15000）。 */
  reconnectMaxDelayMs?: number
  /** 重连最多尝试次数（不传 = 用桥自己的默认 0 = 无限后台重连）。 */
  reconnectMaxAttempts?: number
}

/** 挂载 host 半插件：**一次 `mountHostPlugin`**，参数全来自 GP 自己的参数家。 */
export async function apply(ctx: Context, config: Config = {}): Promise<void> {
  await mountHostPlugin(ctx, {
    // `logLabel` 同时就是**服务名**（骨架把这两个词并成一个，避免同一个词写两遍）；
    // GP 的 `serverName`（工具命名空间）与它同值（'paperpilot'），但语义是另一件事，故仍分开给。
    serverName: config.serverName || GP_TOOLS.serverName,
    logLabel: GP_BRIDGE.logLabel,
    // 面板端口文件所在的项目根：默认 dsh 进程 cwd（launcher 已把 cwd 钉在项目根）。
    projectRoot: config.projectRoot?.trim() || process.cwd(),
    // 端点**每次建会话现解**（骨架只存这个函数，不缓存结果）——GP 的 MCP 端点由框架以
    // `port=0` 起，权威重启会换端口；固化 URL 就会对着旧端口永久重试。
    resolveUrl: () => gpResolveHubUrl(config),
    clientInfo: { ...GP_BRIDGE.clientInfo },
    // 远程鉴权头（本地 hubToken 空 ⇒ `buildRequestInit` 回 undefined，不带头）。
    requestInit: buildRequestInit(config),
    offlineHint: GP_BRIDGE.offlineHint,
    // 三个旋钮**只在部署显式给了值时才覆盖**（不给 = undefined ⇒ 用桥自己的默认，别手抄第二份）。
    reconnect: {
      initialDelayMs: config.reconnectInitialDelayMs,
      maxDelayMs: config.reconnectMaxDelayMs,
      maxAttempts: config.reconnectMaxAttempts,
    },
    // 面板参数：host 半注入一次（client 半在 `src/client/index.ts` 另注入一次，各带 disposer）。
    panelConfig: { ...GP_PANEL },
    // ⚠ **不传 `registerCommand`**：GP 没有看门人（没有进程轮询 `.mode-request`），
    //   交权按钮与 command 都不该存在（无消费者 = 不留"按了没反应"的死缝）。与既有结论一致。
  })
}
