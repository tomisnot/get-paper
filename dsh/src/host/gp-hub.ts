/**
 * GP 的 **node 侧端点知识**：把 GP 的值（`gp-params.ts`）交给资产包。
 *
 * ⚠ **2026-10-01 瘦身**：`gpSessionFactory` / `gpBridgeOptions` 已删——建会话工厂、桥参数装配
 * （`logLabel` / `offlineHint` / `clientInfo` / `requestInit` / logger 转发 / `onTools` swap）
 * 现在由资产的骨架 `mountHostPlugin` 统一接管（见 `src/index.ts`）。
 * 本文件只剩下**只有 GP 知道的那件事**：端点怎么算（显式 URL > GP 的端口文件 > GP 的默认端点）。
 *
 * ⚠ 本文件**不是**资产件，别把它的内容想成"该跟上游同步"的东西；它随 GP 的部署形态变。
 */
import type { HubUrlConfig } from '@mecha/dsh-panel/host/config.ts'
import { resolveHubUrl } from '@mecha/dsh-panel/host/config.ts'
import { GP_BRIDGE } from '../gp-params.ts'

/** GP 的 dsh 插件配置里与端点有关的那两个（用户在 cordis patch 的 entry `config:` 里给）。 */
export interface GpHubConfig {
  /** 显式端点（远程设备用）；给了就不读端口文件。 */
  hubUrl?: string
  /** 端口文件路径；缺省用 GP 的 `.mcp-port`（见 `GP_BRIDGE.mcpPortFile`）。 */
  mcpPortFile?: string
}

/**
 * GP 填给资产 `resolveHubUrl` 的完整参数（**纯函数**，可单测）。
 *
 * 资产的口径是"显式 URL > 端口文件 > 项目默认，**缺省不猜端口**"——所以两个项目值
 * （端口文件名、默认端点）必须由 GP 显式给；这就是本函数存在的全部理由。
 */
export function gpHubUrlConfig(config: GpHubConfig = {}): HubUrlConfig {
  return {
    hubUrl: config.hubUrl,
    mcpPortFile: config.mcpPortFile?.trim() || GP_BRIDGE.mcpPortFile,
    defaultUrl: GP_BRIDGE.defaultUrl,
  }
}

/**
 * GP 的端点解析。
 *
 * ⚠ 调用点把它作为**函数**交给骨架（不是字符串）：骨架每次建会话现解 ⇒ 权威重启换了端口
 * （GP 的 MCP 端点由框架 `port=0` 起）后，下一拍就读到新端口，而不是固化在旧 URL 上。
 */
export async function gpResolveHubUrl(config: GpHubConfig = {}): Promise<string> {
  return resolveHubUrl(gpHubUrlConfig(config))
}
