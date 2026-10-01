/**
 * `@modelcontextprotocol/sdk` 的**最小类型垫片**（声明式，无运行时代码）。
 *
 * ⚠ **为什么需要它**：本资产要在**框架仓独立类型检查**（`checks/dsh_panel_selfcheck.py`
 * 的第二关借消费者的 `tsc` 跑 `--strict --noUncheckedIndexedAccess`），而框架仓
 * **不装** `@modelcontextprotocol/sdk`（它是**用桥的项目**才需要的依赖）。
 * ⇒ 这里声明"本资产真正用到的那一小片"，让桥在框架仓里**类型可查**。
 *
 * ⚠ **它不是运行时依赖**：本文件只有 `declare`，编译后被完全擦除。
 * 运行时仍由**项目**的 `node_modules` 提供真 SDK（与 dsh 类型垫片同一姿势：
 * 形状取自真源码，只声明用到的那一片）。
 *
 * ⚠ **口径依赖（如实写）**：签名写成"本资产实际调用姿势"的形状，因此**比真 SDK 宽松**。
 * 真 SDK 若改了这几个签名，这里不会自动红 ⇒ 与 dsh 垫片同族风险，
 * 兜底是**用桥的项目的 `npm run typecheck` + 真机冒烟**（不是本垫片）。
 */

declare module '@modelcontextprotocol/sdk/client/index.js' {
  /** 工具列表项（只声明本资产读的三个键）。 */
  export interface SdkTool {
    name: string
    description?: string
    inputSchema?: unknown
  }

  export interface SdkListToolsResult {
    tools?: SdkTool[]
  }

  export interface SdkCallToolResult {
    content?: unknown
    isError?: boolean
  }

  /** 会话传输面（本资产只读 `sessionId`、写 `onclose`、调 `close`）。 */
  export interface SdkTransport {
    sessionId?: string
    onclose?: () => void
    close(): Promise<void>
  }

  export class Client {
    constructor(info: { name: string; version: string }, options?: unknown)
    connect(transport: SdkTransport): Promise<void>
    listTools(): Promise<SdkListToolsResult>
    callTool(
      params: { name: string; arguments: Record<string, unknown> },
      resultSchema?: unknown,
      options?: { signal?: AbortSignal },
    ): Promise<SdkCallToolResult>
    close(): Promise<void>
  }
}

declare module '@modelcontextprotocol/sdk/client/streamableHttp.js' {
  import type { SdkTransport } from '@modelcontextprotocol/sdk/client/index.js'

  export interface StreamableHTTPClientTransportOptions {
    requestInit?: RequestInit
    fetch?: typeof globalThis.fetch
  }

  export class StreamableHTTPClientTransport implements SdkTransport {
    constructor(url: URL, options?: StreamableHTTPClientTransportOptions)
    sessionId?: string
    onclose?: () => void
    close(): Promise<void>
  }
}
