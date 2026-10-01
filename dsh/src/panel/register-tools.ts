/**
 * 把桥发现的 MCP 工具**原生注册**进宿主的工具表（`ctx.tools`）。
 *
 * 姿势照抄 dsh 自己的 `packages/mcp/mcp-client/src/tools.ts`（本资产正是替换它那个坏掉
 * HTTP 重连的客户端）：`ToolDefinition = { name: mcp__<server>__<raw>, description,
 * parameters (= MCP inputSchema 直接透传), output:{schema,render}, execute }`
 * → `ctx.tools.register(def)` 返回 disposer。**两阶段 swap**：先建新一代定义、全部注册
 * 成功后再 dispose 旧的（避免工具集在重连瞬间出现半拉子）。
 *
 * execute 是**透传**：转发到桥（桥负责自愈重连）；MCP `isError` → 抛错（宿主 ToolRuntime
 * 据此产出 isError 结果给模型），否则返回规范化 `{content}`。
 *
 * ⚠ **本文件对宿主类型零 import**：宿主（dsh）的类型包是**宿主提供的 peer**，
 * out-of-tree 插件里未必解析得到，而共享资产**必须能在框架仓独立类型检查**。
 * 因此这里用**结构化最小接口**（`HostToolPack` / `HostToolDefinition` …）描述"我们真正用到的那一小片"，
 * 由 TypeScript 的结构化类型自动接受项目传进来的真 `Context`。
 * ⇒ 资产**零 `@deepseek-ai/*` 依赖**（连类型都没有），换宿主版本时不必跟着改。
 */
import type { MechaMcpBridge, ToolDef } from './mcp-bridge.ts'

/** 已注册工具的 disposer 表（public name → 注销函数）。 */
export type ToolDisposers = Map<string, () => void>

/** 宿主工具表：只声明我们用到的 `register`。 */
export interface HostToolRegistry {
  register(def: unknown): () => void
}

/** 宿主日志面（全可选，与 `BridgeLogger` 同形）。 */
export interface HostLogger {
  info?(msg: string): void
  warn?(msg: string): void
  error?(msg: string): void
}

/**
 * `syncTools` 需要的**宿主最小面**：一张工具表 + 一个日志口。
 * 项目的真 `Context`（dsh 的 `Context`）结构上就满足它。
 */
export interface HostToolPack {
  tools: HostToolRegistry
  logger?: HostLogger
}

/** 宿主工具定义：只声明本资产构造得出的那几个键。 */
export interface HostToolDefinition {
  name: string
  description: string
  parameters: Record<string, unknown>
  output: {
    schema: Record<string, unknown>
    render(args: unknown, value: unknown): Array<{ type: 'text'; text: string }>
  }
  execute(args: unknown, exec: { signal?: AbortSignal }): Promise<{ content: unknown[] }>
}

/** `syncTools` 的可选项（**项目值**）。 */
export interface SyncToolsOptions {
  /** 日志前缀标签；缺省 `[mcp-bridge]`，与桥同源（项目通常传同一个值）。 */
  logLabel?: string
}

/** `mcp__<serverName>__<rawName>`（与 dsh mcp-client 同一命名规约；serverName ∈ [A-Za-z0-9_-]）。 */
export function publicToolName(serverName: string, rawName: string): string {
  return `mcp__${serverName}__${rawName}`
}

/** 从 MCP content 块里抽出可读文本（trust boundary：块形状可能不齐，宽松处理）。 */
function extractText(content: unknown, rawName: string): string {
  if (!Array.isArray(content)) return `(${rawName}: 无输出)`
  const texts: string[] = []
  for (const block of content) {
    const b = block as { type?: unknown; text?: unknown } | null
    if (b && b.type === 'text' && typeof b.text === 'string') texts.push(b.text)
  }
  return texts.length > 0 ? texts.join('\n') : `(${rawName}: 无文本输出)`
}

/** 为一个 MCP 工具构造宿主 ToolDefinition（execute 透传到桥）。 */
export function buildDefinition(
  bridge: MechaMcpBridge,
  serverName: string,
  tool: ToolDef,
): HostToolDefinition {
  const raw = tool.name
  const parameters = (tool.inputSchema ?? { type: 'object', properties: {}, additionalProperties: true }) as Record<string, unknown>
  return {
    name: publicToolName(serverName, raw),
    description: tool.description ?? '',
    parameters,
    output: {
      schema: {
        type: 'object',
        properties: { content: { type: 'array', items: {} } },
        required: ['content'],
        additionalProperties: false,
      },
      render(_args: unknown, value: unknown) {
        const v = value as { content?: unknown } | null
        return [{ type: 'text', text: extractText(v?.content, raw) }]
      },
    },
    execute: async (args: unknown, exec: { signal?: AbortSignal }) => {
      const argsObj = (typeof args === 'object' && args !== null ? args : {}) as Record<string, unknown>
      // 桥在此处自愈：session 失效/连不上 → 重连重试；仍不行 → 抛可读"服务离线"错误。
      const result = await bridge.callTool(raw, argsObj, exec.signal) as
        | { content?: unknown; isError?: boolean }
        | null
      const text = extractText(result?.content, raw)
      if (result?.isError === true) throw new Error(text)
      return { content: Array.isArray(result?.content) ? result.content : [] }
    },
  }
}

/**
 * 两阶段 swap：用 `tools` 建新一代定义并注册，成功后 dispose `previous`。
 * @returns 新一代的 disposer 表（供下次 swap 或 teardown 用）。
 */
export function syncTools(
  ctx: HostToolPack,
  bridge: MechaMcpBridge,
  serverName: string,
  tools: ToolDef[],
  previous: ToolDisposers,
  options: SyncToolsOptions = {},
): ToolDisposers {
  const tag = `[${options.logLabel?.trim() || 'mcp-bridge'}]`
  // Phase 1：先构造全部定义（不碰注册表）。
  const defs = tools.map(tool => buildDefinition(bridge, serverName, tool))
  // Phase 2：注册新一代，成功后再拆旧的。
  const next: ToolDisposers = new Map()
  try {
    for (const def of defs) next.set(def.name, ctx.tools.register(def))
  } catch (error) {
    for (const dispose of next.values()) { try { dispose() } catch { /* ignore */ } }
    ctx.logger?.error?.(`${tag} 工具注册失败（命名冲突？）：${String(error)}`)
    return previous   // 保留旧一代，不制造半拉子
  }
  for (const dispose of previous.values()) { try { dispose() } catch { /* ignore */ } }
  return next
}
