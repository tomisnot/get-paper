/**
 * 服务端点解析（纯逻辑，无 dsh 依赖，可单测）。
 *
 * 优先级：显式 `serviceUrl` > 读**端口文件**（由启动方写）> **项目给的默认端点**。
 * 端口文件格式容错：裸端口 / 完整 URL / 含数字的任意串。
 *
 * ⚠ **三样都是项目值，资产不猜**：端口文件名、默认端点、显式 URL。
 * 本模块只拥有**规则**（"显式 > 文件 > 默认"这条优先级与容错解析），
 * 与 `mecha/portfile.py` 同一形状：**库拥有规则，宿主拥有名字**。
 */
import { readFile } from 'node:fs/promises'

export interface HubUrlConfig {
  /** 显式端点（远程设备用）；给了就不读端口文件。 */
  hubUrl?: string
  /** 端口文件路径（启动方写）；缺省不读文件。 */
  mcpPortFile?: string
  /** 端口文件读不到、也没给显式 URL 时的默认端点（**项目值**）。缺省返回空串（不猜端口）。 */
  defaultUrl?: string
  /** 端点路径（`http://host:port` 之后那段），默认 `/mcp`。 */
  path?: string
}

/** 端口文件里抽端点的规则：完整 URL 直接用；否则取第一段 2–5 位数字当端口。 */
function endpointFromPortFile(raw: string, path: string): string | null {
  const text = raw.trim()
  if (/^https?:\/\//i.test(text)) return text
  const m = text.match(/(\d{2,5})/)
  return m && m[1] ? `http://127.0.0.1:${m[1]}${path}` : null
}

/** 解析服务的 MCP 端点 URL。绝不抛（解析不到就回落项目默认，交由桥的重连去处理"连不上"）。 */
export async function resolveHubUrl(config: HubUrlConfig): Promise<string> {
  const path = config.path?.trim() || '/mcp'
  const explicit = config.hubUrl?.trim()
  if (explicit) return explicit
  const portFile = config.mcpPortFile?.trim()
  if (portFile) {
    try {
      const found = endpointFromPortFile(await readFile(portFile, 'utf8'), path)
      if (found) return found
    } catch {
      /* 没有端口文件（服务未起 / cwd 不对）→ 回落默认，桥会后台重连 */
    }
  }
  return config.defaultUrl?.trim() || ''
}

export interface RemoteConfig {
  /** 远程 Hub 的鉴权 token（→ `Authorization: Bearer`）。本地默认空=不带。 */
  hubToken?: string
  /** 额外请求头（自定义鉴权/追踪）。 */
  hubHeaders?: Record<string, string>
}

/**
 * 由远程配置构造 fetch 的 `RequestInit`（鉴权头）。纯函数、可单测。
 *
 * 无 token/头 → `undefined`（本地默认，不带头）。这是"远程跨设备"（需求③）的插件侧
 * 接缝：将来 Hub 的写通道若经 L3 双闸安全地远程暴露（T6 今天写通道仍只 localhost），
 * 插件带上 token 即可连远程 Hub；`cockpitUrl` 同理可指向远程 cockpit-server（读通道
 * 早已支持 `--host 0.0.0.0 --token`）。
 */
export function buildRequestInit(config: RemoteConfig): RequestInit | undefined {
  const headers: Record<string, string> = { ...(config.hubHeaders ?? {}) }
  if (config.hubToken) headers.Authorization = `Bearer ${config.hubToken}`
  return Object.keys(headers).length > 0 ? { headers } : undefined
}
