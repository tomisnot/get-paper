/**
 * paperpilot dsh 插件 —— **HOST 入口**（Cordis 插件，函数形态）。
 *
 * 职责（AI 侧集成轨，DESIGN.md §17）：
 *  1. 建自愈 MCP 桥连到 PaperPilot 的语义通道——接管 dsh stock HTTP 桥缺的重连
 *     （治"服务重启即永久 404"死区）。
 *  2. 桥发现工具 → `syncTools` 原生注册进 `ctx.tools`（`mcp__paperpilot__<tool>`）；重连后
 *     工具集变化会再同步。
 *  3. 断联显式信号：状态迁移写日志；服务离线时工具调用抛**可读**离线错误（桥内已实现）。
 *  4. 注册**面板地址的同源只读路由**：读项目根 `PANEL_CONFIG.PORT_FILE` 里的裸端口 → 回
 *     `{base}`（**绝不回落默认端口**），client 半据此把面板挂进 dsh 右栏。
 *
 * ⚠ **本文件不再自持第二份实现**（2026-10-01）：桥 / 会话工厂 / 端点解析 / 工具注册 / 面板地址
 * 五件**全部**取自 mecha 的共享资产 `mecha/dsh-panel/`，逐字复制在 `./panel/`；
 * **GP 的值只住在 `gp-params.ts`（浏览器安全）与 `host/gp-hub.ts`（node-only）**，
 * 经参数注入（参数表在框架侧 `mecha/dsh-panel/README.md`；类名 `MechaMcpBridge` **不是**项目参数）。
 *
 * 安全红线：**只 connect、绝不 spawn 服务**（人启动 launcher = 权威）。换 harness 只丢
 * 本插件，PaperPilot 的独立 MCP server 照用（跨 harness）。
 */
import type { Context } from '@deepseek-ai/cordis'
import { MechaMcpBridge } from './panel/mcp-bridge.ts'
import { buildRequestInit } from './panel/config.ts'
import { syncTools, type ToolDisposers } from './panel/register-tools.ts'
import { gpBridgeOptions, gpResolveHubUrl, gpSessionFactory } from './host/gp-hub.ts'
import { GP_BRIDGE, GP_TOOLS } from './gp-params.ts'
import { makeMonitorUrlHandler } from './panel/monitor-url.ts'
import { PANEL_CONFIG } from './panel/panel-config.ts'

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
 *
 * ⚠ 字段名与共享资产同词（`hubUrl` / `hubToken` / `hubHeaders`）：GP 自建版曾叫 `mcpUrl` /
 * `mcpToken` / `mcpHeaders`——改名的理由只有一个：**同一个概念在资产与本仓只有一个名字**
 * （没有任何 cordis patch 给本插件传过 `config:`，故改名对运行期零影响）。
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
  /** 重连退避：首次延迟 ms（不传 = 用资产的默认 300）。 */
  reconnectInitialDelayMs?: number
  /** 重连退避：上限 ms（不传 = 用资产的默认 15000）。 */
  reconnectMaxDelayMs?: number
  /** 重连最多尝试次数（不传 = 用资产的默认 0 = 无限后台重连）。 */
  reconnectMaxAttempts?: number
}

/** 挂载 host 桥：连服务、注册工具、状态信号；teardown 时注销工具并关桥。 */
export async function apply(ctx: Context, config: Config = {}): Promise<void> {
  await ctx.effect(async () => {
    const serverName = config.serverName || GP_TOOLS.serverName
    let disposers: ToolDisposers = new Map()

    // 端点**每次建会话现解**（不固化）：PaperPilot 的 MCP 端点由框架以 `port=0` 起，
    // 权威重启后端口会变 ⇒ 若把 URL 固化在装配期，桥只会对着旧端口永久重试。
    // 显式 hubUrl（远程设备）在 resolveHubUrl 里优先级最高，行为不变。
    // lastUrl 仅供日志展示最近一次解析结果。
    let lastUrl = ''
    const resolveUrl = async (): Promise<string> => {
      lastUrl = await gpResolveHubUrl(config)
      return lastUrl
    }

    const bridge = new MechaMcpBridge(gpBridgeOptions(
      // requestInit：远程鉴权头（本地 hubToken 空 → 不带头）。
      gpSessionFactory(resolveUrl, buildRequestInit(config)),
      {
        // 三个重连旋钮**只在部署显式给了值时才覆盖**（不给 = undefined ⇒ 用资产自己的
        // 默认值；GP 不再手抄一份 300/15000/0 —— 那是第二处真值）。
        reconnect: {
          initialDelayMs: config.reconnectInitialDelayMs,
          maxDelayMs: config.reconnectMaxDelayMs,
          maxAttempts: config.reconnectMaxAttempts,
        },
        logger: {
          info: m => ctx.logger?.info?.(m),
          warn: m => ctx.logger?.warn?.(m),
          error: m => ctx.logger?.error?.(m),
        },
        // 桥发现/刷新工具 → 两阶段 swap 重注册（首连与每次重连后都走这里）。
        onTools: tools => {
          disposers = syncTools(ctx, bridge, serverName, tools, disposers,
                                { logLabel: GP_BRIDGE.logLabel })
        },
        // 断联显式信号：状态迁移落日志，人/AI 不再面对"工具在但静默坏"。
        onStatus: status => {
          if (status === 'offline') ctx.logger?.warn?.('[paperpilot] PaperPilot 离线：工具暂不可用，后台重连中')
          else if (status === 'ready') ctx.logger?.info?.(`[paperpilot] 已连上 PaperPilot MCP：${lastUrl}`)
          else if (status === 'reconnecting') ctx.logger?.warn?.('[paperpilot] 与 PaperPilot 的连接中断，重连中…')
        },
      },
    ))

    // 面板地址的**同源只读路由**（共享资产 `panel/monitor-url.ts`，逐字复制）：
    // 读项目根 `PANEL_CONFIG.PORT_FILE` 里的裸端口 → `{base}`。**绝不回落默认端口**
    // （回落会把"Web 没起来"显示成"连上了但空白"）；每次被 fetch 都现读文件 ⇒ Web 换端口后
    // 下一拍落在新端口，不需要重启 dsh。
    //
    // 为什么用 `ctx.inject` 而不是顶层 `inject: [..., 'webServer']`：本插件要**在 headless
    // 下照常工作**（`paperpilot mcp` 不进 dsh；但换 harness/无 web 时 MCP 桥仍要活）。顶层
    // inject 缺一个服务就**整个插件不激活**，那会把"面板没有 web 服务"升级成"AI 工具也没了"。
    // `ctx.inject` 只在 webServer 就绪时才跑回调，且返回值即 disposer。
    ctx.inject?.(['webServer'], scoped => {
      const webServer = (scoped as { webServer: { register(route: {
        kind: 'exact'; path: string
        // 参数用 `any`：`unknown` 与本插件实现的 `IncomingMessage`/`ServerResponse` 逆变不兼容
        handler: (req: any, res: any) => void }): () => void } }).webServer
      const root = config.projectRoot?.trim() || process.cwd()
      const disposeRoute = webServer.register({
        kind: 'exact',
        path: PANEL_CONFIG.ROUTE_PATH,
        handler: makeMonitorUrlHandler({ root, portFile: PANEL_CONFIG.PORT_FILE }),
      })
      ctx.logger?.info?.(
        `[paperpilot] 注册面板地址路由 ${PANEL_CONFIG.ROUTE_PATH}` +
        `（读 ${root}\\${PANEL_CONFIG.PORT_FILE}，不回落默认端口）`)
      return disposeRoute
    })

    ctx.logger?.info?.('[paperpilot] 挂载 PaperPilot MCP 桥（端点每次建会话现解 .mcp-port，只 connect 不 spawn 权威）')
    await bridge.start()   // 服务未起也不抛：插件照常加载，桥后台重连，起来后自动注册工具

    return async () => {
      for (const dispose of disposers.values()) {
        try { dispose() } catch { /* ignore */ }
      }
      await bridge.close()
    }
  }, 'paperpilot: host MCP bridge')
}
