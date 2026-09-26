/**
 * PaperPilot MCP 端点解析（纯逻辑，无 dsh 依赖，可单测）。
 *
 * 优先级：显式 `mcpUrl` > 读 `.mcp-port` 端口文件（launcher `paperpilot ai` 写）> 默认 8780。
 * 端口文件格式容错：裸端口 / 完整 URL / 含数字的任意串。
 */
import { readFile } from 'node:fs/promises'

export interface McpUrlConfig {
  mcpUrl?: string
  mcpPortFile?: string
}

const DEFAULT_URL = 'http://127.0.0.1:8780/mcp'

/** 解析 PaperPilot 的 MCP 端点 URL。绝不抛（解析不到就回落默认，交由桥的重连去处理"连不上"）。 */
export async function resolveMcpUrl(config: McpUrlConfig): Promise<string> {
  const explicit = config.mcpUrl?.trim()
  if (explicit) return explicit
  const portFile = config.mcpPortFile?.trim() || '.mcp-port'
  try {
    const raw = (await readFile(portFile, 'utf8')).trim()
    if (/^https?:\/\//i.test(raw)) return raw
    const m = raw.match(/(\d{2,5})/)
    if (m && m[1]) return `http://127.0.0.1:${m[1]}/mcp`
  } catch {
    /* 没有端口文件（服务未起 / cwd 不对）→ 回落默认，桥会后台重连 */
  }
  return DEFAULT_URL
}

export interface RemoteConfig {
  /** 远程 MCP 的鉴权 token（→ Authorization: Bearer）。本地默认空=不带。 */
  mcpToken?: string
  /** 额外请求头（自定义鉴权/追踪）。 */
  mcpHeaders?: Record<string, string>
}

/**
 * 由远程配置构造 fetch 的 `RequestInit`（鉴权头）。纯函数、可单测。
 *
 * 无 token/头 → `undefined`（本地默认，不带头）。这是"远程跨设备"的插件侧接缝：
 * MCP 写通道今天只 localhost（安全红线），token 是给将来受控远程暴露留的。
 */
export function buildRequestInit(config: RemoteConfig): RequestInit | undefined {
  const headers: Record<string, string> = { ...(config.mcpHeaders ?? {}) }
  if (config.mcpToken) headers.Authorization = `Bearer ${config.mcpToken}`
  return Object.keys(headers).length > 0 ? { headers } : undefined
}
