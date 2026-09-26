/**
 * paperpilot dsh 插件 —— **HOST 入口**（Cordis 插件，函数形态）。
 *
 * 职责（AI 侧集成轨，DESIGN.md §17）：
 *  1. 建自愈 MCP 桥连到 PaperPilot 的语义通道（`httpSessionFactory` + `PaperPilotMcpBridge`）——
 *     接管 dsh stock HTTP 桥缺的重连（治"服务重启即永久 404"死区）。
 *  2. 桥发现工具 → `syncTools` 原生注册进 `ctx.tools`（`mcp__paperpilot__<tool>`）；重连后
 *     工具集变化会再同步。
 *  3. 断联显式信号：状态迁移写日志；服务离线时工具调用抛**可读**离线错误（桥内已实现）。
 *  4. 向 web client 注入 bootstrap（`__PAPERPILOT__`），client 半据此把 PaperPilot 面板
 *     iframe 进 dsh 右栏。
 *
 * 安全红线：**只 connect、绝不 spawn 服务**（人启动 launcher = 权威）。换 harness 只丢
 * 本插件，PaperPilot 的独立 MCP server 照用（跨 harness）。
 */
import type { Context } from '@deepseek-ai/cordis'
import type { IndexInjection } from '@deepseek-ai/dsh-host-webserver'
import { PaperPilotMcpBridge } from './host/mcp-bridge.ts'
import { httpSessionFactory } from './host/mcp-session-http.ts'
import { buildRequestInit, resolveMcpUrl } from './host/config.ts'
import { syncTools, type ToolDisposers } from './host/register-tools.ts'

/** Cordis 插件显示名（诊断用；工具命名空间默认同此）。 */
export const name = 'paperpilot'

/** 等待 dsh 的 tools 服务就绪再激活（工具注册需要它）。 */
export const inject = ['tools']

/**
 * 插件配置（用户在 cordis.patch.yml 的 entry `config:` 里给；全部可选，缺省在 apply 里兜）。
 *
 * 刻意**不导出 schemastery 的 Config 校验 schema**：那会引入对 `@deepseek-ai/schemastery` 的
 * **运行时** import，而 out-of-tree 插件的 node_modules 里没有它（它是 dsh 宿主提供的 peer）。
 * 去掉后，host 半对 @deepseek-ai/* **只剩类型 import（编译期擦除）**、运行时零 peer 依赖。
 */
export interface Config {
  /** 显式 PaperPilot MCP 端点；留空则从 mcpPortFile 解析。 */
  mcpUrl?: string
  /** 端口文件路径（launcher 写；默认项目根 `.mcp-port`，相对 dsh 的 cwd）。 */
  mcpPortFile?: string
  /** 工具命名空间：`mcp__<serverName>__<tool>`。 */
  serverName?: string
  /** PaperPilot Web 面板地址（client 面板 iframe 它）。 */
  webUrl?: string
  /** 远程 MCP 鉴权 token（→ Authorization: Bearer）；本地默认空。 */
  mcpToken?: string
  /** 额外请求头。 */
  mcpHeaders?: Record<string, string>
  /** 重连退避：首次延迟 ms（默 300）。 */
  reconnectInitialDelayMs?: number
  /** 重连退避：上限 ms（默 15000）。 */
  reconnectMaxDelayMs?: number
  /** 重连最多尝试次数（0=无限后台重连）。 */
  reconnectMaxAttempts?: number
}

/** 挂载 host 桥：连服务、注册工具、状态信号；teardown 时注销工具并关桥。 */
export async function apply(ctx: Context, config: Config = {}): Promise<void> {
  await ctx.effect(async () => {
    const url = await resolveMcpUrl(config)
    const serverName = config.serverName || 'paperpilot'
    const webUrl = config.webUrl || 'http://127.0.0.1:8080/'
    let disposers: ToolDisposers = new Map()

    const bridge = new PaperPilotMcpBridge({
      // requestInit：远程鉴权头（本地 mcpToken 空 → 不带头）。
      sessionFactory: httpSessionFactory({ url, requestInit: buildRequestInit(config) }),
      reconnect: {
        initialDelayMs: config.reconnectInitialDelayMs ?? 300,
        maxDelayMs: config.reconnectMaxDelayMs ?? 15000,
        maxAttempts: config.reconnectMaxAttempts ?? 0,
      },
      logger: {
        info: m => ctx.logger?.info?.(m),
        warn: m => ctx.logger?.warn?.(m),
        error: m => ctx.logger?.error?.(m),
      },
      // 桥发现/刷新工具 → 两阶段 swap 重注册（首连与每次重连后都走这里）。
      onTools: tools => {
        disposers = syncTools(ctx, bridge, serverName, tools, disposers)
      },
      // 断联显式信号：状态迁移落日志，人/AI 不再面对"工具在但静默坏"。
      onStatus: status => {
        if (status === 'offline') ctx.logger?.warn?.('[paperpilot] PaperPilot 离线：工具暂不可用，后台重连中')
        else if (status === 'ready') ctx.logger?.info?.(`[paperpilot] 已连上 PaperPilot MCP：${url}`)
        else if (status === 'reconnecting') ctx.logger?.warn?.('[paperpilot] 与 PaperPilot 的连接中断，重连中…')
      },
    })

    // 向 web client 的 index.html 注入 bootstrap（client 面板据此 iframe PaperPilot Web）。
    // 只**监听**事件、不 inject webServer：headless（无 web）时事件不触发，MCP 桥照常工作。
    const offInject = ctx.on('webserver/index-inject', (table: IndexInjection[]) => {
      table.push({ kind: 'global', name: '__PAPERPILOT__',
        value: { webUrl } })
    })

    ctx.logger?.info?.(`[paperpilot] 连接 PaperPilot MCP：${url}（只 connect，不 spawn 权威）`)
    await bridge.start()   // 服务未起也不抛：插件照常加载，桥后台重连，起来后自动注册工具

    return async () => {
      offInject()
      for (const dispose of disposers.values()) {
        try { dispose() } catch { /* ignore */ }
      }
      await bridge.close()
    }
  }, 'paperpilot: host MCP bridge')
}
