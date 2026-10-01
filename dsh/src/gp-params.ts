/**
 * GP 的**参数家**（本项目专有；**不进共享资产指纹表**）。
 *
 * ## 为什么单独立一个文件
 *
 * 共享资产 `panel/` 把"可变量"收进它自己的参数块（`panel/panel-config.ts`），而**资产覆盖
 * 不到的项目值**——MCP 桥的日志前缀 / 断联指引 / 客户端标识 / 端点默认 / 端口文件名——
 * 也需要**一个家**。散在调用点上就是两处真值（R1 / D1），本工程为此付过代价。
 *
 * ## 硬约束：本文件必须**浏览器安全**
 *
 * 零 import、零 `node:*`（与资产的 `panel-config.ts` 同一条纪律）。需要读文件或碰 SDK 的
 * 那一半在 `host/gp-hub.ts`（node-only）。
 *
 * ## 与资产的关系
 *
 * 资产件（`panel/mcp-bridge.ts` 等）一律**逐字复制**；GP 的值**只在这里与 `host/gp-hub.ts`
 * 出现**，经参数传进去 ⇒ "逐字复制"才成立（参数表见 `panel/README.md` 的「参数表」）。
 */

/** GP 填给共享资产 opt-in 件的值（**唯一来源**）。 */
export const GP_BRIDGE = {
  /** `BridgeOptions.logLabel`：日志前缀 `[paperpilot] …`（与 GP 自建版的日志前缀逐字一致）。 */
  logLabel: 'paperpilot',
  /**
   * `BridgeOptions.offlineHint`：断联时那句**可操作指引**（项目知识：怎么把软件重新起来）。
   * 自建版把这段写死在桥的错误文案里；资产把它参数化了 ⇒ 必须由 GP 显式传，
   * 否则退回中性句（"请确认软件已启动…"），我们就丢了"人经 `paperpilot ai` 起 launcher"这条指引。
   */
  offlineHint: '请确认 launcher（paperpilot ai / paperpilot mcp）在跑；工具仍在列表但暂不可用。',
  /** `HttpSessionOptions.clientInfo`：initialize 上报的客户端标识（原自建版的字面量）。 */
  clientInfo: { name: 'paperpilot-dsh', version: '0.1.0' },
  /** `HubUrlConfig.defaultUrl`：端口文件读不到时的默认端点（原自建版的 `DEFAULT_URL`；端口的家在 `config/settings.yaml` 的 `mcp.port`）。 */
  defaultUrl: 'http://127.0.0.1:8780/mcp',
  /** `HubUrlConfig.mcpPortFile`：GP 的 launcher 写的 MCP 端口文件名（原自建版的默认值）。 */
  mcpPortFile: '.mcp-port',
} as const

/** 工具命名空间（**唯一来源**）：公开名 = `mcp__<serverName>__<rawName>`。 */
export const GP_TOOLS = {
  serverName: 'paperpilot',
} as const
