/**
 * httpSessionFactory —— 用官方 MCP SDK 实现的 `SessionFactory`（streamable-http 传输）。
 *
 * 这是**唯一** import `@modelcontextprotocol/sdk` 的地方；`mcp-bridge.ts` 只认 `McpSession`
 * 抽象（纯逻辑、可单测）。分离的目的：重连状态机与 SDK 接线各自独立演进、各自可测。
 *
 * **重连恢复的本质在这里**：工厂每次被调用都 `new` 一个 transport + client 并 `connect()`
 * ——connect 会自动跑 initialize 流程，服务端据此下发**新的** `Mcp-Session-Id`。所以桥的
 * "重连"= 调本工厂拿一个全新会话，绝不复用失效的旧 session id（这正是 dsh stock 桥缺的、
 * 导致 §2.3 永久 404 的那一步）。
 */
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { McpSession, SessionFactory, ToolDef } from './mcp-bridge.ts'

export interface HttpSessionOptions {
  /**
   * 服务的 MCP 端点，如 `http://127.0.0.1:<项目端口文件里那个端口>/mcp`。**也可以是异步解析器**：
   * 每次建会话（首连与每次重连）都重新求值 ⇒ 权威重启换端口后，工厂重读
   * 端口文件就连上新端口（实测：56093→52755 漂移曾致老会话永久失联）。
   * 传字符串则固化——只有显式 hubUrl（远程设备）才该这样用。
   * ⚠ 端口文件叫什么、默认端点是什么，**都是项目值**（见 `config.ts` 的 `resolveServiceUrl`）。
   */
  url: string | (() => Promise<string>)
  /**
   * 客户端标识（initialize 时上报）。缺省给资产自己的中性名
   * ⇒ **资产零项目字面量**；项目要自己的名字就显式传。
   */
  clientInfo?: { name: string; version: string }
  /** 额外请求头（如远程只读鉴权 token）。 */
  requestInit?: RequestInit
  /** 可注入的 fetch（测试 / 代理）。 */
  fetch?: typeof globalThis.fetch
}

/** 造一个"每次调用都新建 streamable-http 会话"的工厂。 */
export function httpSessionFactory(opts: HttpSessionOptions): SessionFactory {
  return async (): Promise<McpSession> => {
    // 动态 URL：每次建会话现解（端口漂移自愈的关键一环；见 HttpSessionOptions.url）。
    const url = typeof opts.url === 'function' ? await opts.url() : opts.url
    const transport = new StreamableHTTPClientTransport(new URL(url), {
      requestInit: opts.requestInit,
      fetch: opts.fetch,
      // 刻意不传 sessionId：让服务端下发新的（重连恢复=新 session，不复用旧 id）。
    })
    const client = new Client(opts.clientInfo ?? { name: 'mcp-bridge-dsh', version: '0.1.0' })
    // connect 自动执行 initialize 握手（拿到新 Mcp-Session-Id）；Hub 不在则此处抛连接错误。
    await client.connect(transport)

    let closed = false
    const session: McpSession = {
      get sessionId(): string | undefined {
        return transport.sessionId
      },
      async listTools(): Promise<{ tools: ToolDef[] }> {
        const res = await client.listTools()
        return {
          tools: (res.tools ?? []).map((t): ToolDef => ({
            name: t.name,
            description: t.description,
            inputSchema: t.inputSchema,
          })),
        }
      },
      async callTool(name: string, args: unknown, signal?: AbortSignal): Promise<unknown> {
        const params = { name, arguments: (args ?? {}) as Record<string, unknown> }
        return await client.callTool(params, undefined, signal ? { signal } : undefined)
      },
      async close(): Promise<void> {
        if (closed) return
        closed = true
        try {
          await client.close()
        } finally {
          try {
            await transport.close()
          } catch { /* 已在关闭，忽略 */ }
        }
      },
    }

    // 传输关闭（如 SSE 流断）→ 通知桥做被动重连（补 dsh stock 桥缺的 onclose 路径之一）。
    transport.onclose = () => {
      session.onClose?.()
    }
    return session
  }
}
