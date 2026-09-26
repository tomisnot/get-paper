/**
 * PaperPilotMcpBridge —— 自愈的 MCP 客户端，专治 dsh stock HTTP 桥的"服务重启即永久失联"死区。
 *
 * 背景（移植自 Energy Level re0-mecha-dsh，见其 docs/DSH-MCP现状-给开发者的参考资料.md §2.3）：
 * dsh 的 `dsh-mcp-client` 只在 `transport.onclose` 触发时重连，而 streamable-http 的 POST
 * 失败（服务重启后旧 `Mcp-Session-Id` → 404 "Session not found"）**只抛错、不触发 onclose**，
 * SDK 也不用 404 重置 session ⇒ 客户端永久卡死，只能重启 dsh Host。
 *
 * 本桥接管连接生命周期：任何一次 list/call 遇到"session 失效 / 连不上"，就 **拆旧连接 →
 * 重新 initialize（拿新 session）→ 重试一次**；并有后台退避重连 + 健康探活 + 状态事件。
 *
 * **本模块零 SDK、零 dsh 依赖**（纯逻辑，可注入 fake session 精确单测）；真正的 SDK 接线在
 * `mcp-session-http.ts`。错误识别用鸭子类型（HTTP 状态码为正、JSON-RPC 码为负，不冲突），
 * 故不需要 import `StreamableHTTPError`。
 *
 * ⚠ host 侧由 dsh 用 **Node 原生 strip-types** 加载（只擦类型、不生成代码）：
 * 禁用 TS 参数属性 / enum / namespace / JSX（显式字段 + 构造器赋值）。
 */

/** 一个工具的最小描述（喂给 dsh 的 ctx.tools.register）。 */
export interface ToolDef {
  readonly name: string
  readonly description?: string
  readonly inputSchema?: unknown
}

/** 桥的连接状态（供 dsh 侧做断联显式信号 / 面板灯）。 */
export type BridgeStatus = 'connecting' | 'ready' | 'reconnecting' | 'offline'

/** 一次 MCP 会话的抽象（真实现见 mcp-session-http.ts；测试注入 fake）。 */
export interface McpSession {
  /** 服务端下发的 session id（若有）——仅用于诊断/日志。 */
  readonly sessionId?: string
  listTools(): Promise<{ tools: ToolDef[] }>
  callTool(name: string, args: unknown, signal?: AbortSignal): Promise<unknown>
  close(): Promise<void>
  /** 会话底层传输关闭时的回调（桥据此触发被动重连）。 */
  onClose?: () => void
}

/** 建一次新会话的工厂（每次重连都调它，从而拿到**新** session）。 */
export type SessionFactory = () => Promise<McpSession>

export interface BridgeLogger {
  info?(msg: string): void
  warn?(msg: string): void
  error?(msg: string): void
}

export interface ReconnectOptions {
  /** 首次重连延迟（ms），默认 300。 */
  initialDelayMs?: number
  /** 退避上限（ms），默认 15000。 */
  maxDelayMs?: number
  /** 增长因子，默认 1.5。 */
  growFactor?: number
  /** 单次 callTool 触发的重连最多尝试几次（0/负数=无限），默认无限（后台一直试）。 */
  maxAttempts?: number
}

export interface BridgeOptions {
  /** 建会话的工厂（必填；生产用 httpSessionFactory，测试注入 fake）。 */
  sessionFactory: SessionFactory
  /** 退避重连参数。 */
  reconnect?: ReconnectOptions
  logger?: BridgeLogger
  /** 首次拿到工具 / 工具集变化时触发（dsh 侧据此注册/重注册工具）。 */
  onTools?: (tools: ToolDef[]) => void
  /** 状态迁移时触发（dsh 侧据此点亮断联信号）。 */
  onStatus?: (status: BridgeStatus) => void
  /** 测试可注入的 sleep（默认 setTimeout）。 */
  sleep?: (ms: number) => Promise<void>
}

/** 鸭子类型判"session 失效"：HTTP 404/400/405 或消息含 session 语义（不 import SDK）。 */
export function isSessionInvalidError(err: unknown): boolean {
  const e = err as { code?: unknown; message?: unknown } | null
  const code = e?.code
  if (typeof code === 'number' && (code === 404 || code === 400 || code === 405)) return true
  const msg = String(e?.message ?? err ?? '')
  return /session not found|session expired|invalid session|stale session|\b404\b|bad request/i.test(msg)
}

/** 鸭子类型判"连不上"：node 网络错误码 / fetch failed / 5xx。 */
export function isConnectionError(err: unknown): boolean {
  const e = err as { code?: unknown; message?: unknown; cause?: unknown } | null
  const cause = (e?.cause ?? {}) as { code?: unknown; message?: unknown }
  const parts = [e?.message, e?.code, cause.message, cause.code, err]
  const msg = parts.map(p => String(p ?? '')).join(' ')
  return /ECONNREFUSED|ECONNRESET|ENOTFOUND|EPIPE|EAI_AGAIN|socket hang up|fetch failed|network|terminated|\b50[0-9]\b/i.test(msg)
}

const defaultSleep = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms))

/**
 * 自愈 MCP 桥。生命周期：`start()` 首连（失败不抛，转后台重连，不阻塞插件加载）；
 * `callTool()`/`listTools()` 遇到 session 失效或连不上时自动重连并重试一次；`close()` 停。
 */
export class PaperPilotMcpBridge {
  private session?: McpSession
  private tools: ToolDef[] = []
  private status: BridgeStatus = 'connecting'
  private closed = false
  /** 单飞：并发的重连共用同一个 in-flight promise（避免重连风暴）。 */
  private reconnecting?: Promise<void>
  private readonly reconnect: Required<Pick<ReconnectOptions, 'initialDelayMs' | 'maxDelayMs' | 'growFactor'>> & { maxAttempts: number }
  private readonly sleep: (ms: number) => Promise<void>
  private readonly opts: BridgeOptions

  // 不用 TS 参数属性：dsh strip-only 加载会 ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX。显式字段+赋值。
  constructor(opts: BridgeOptions) {
    this.opts = opts
    const rc = opts.reconnect ?? {}
    this.reconnect = {
      initialDelayMs: rc.initialDelayMs ?? 300,
      maxDelayMs: rc.maxDelayMs ?? 15_000,
      growFactor: rc.growFactor ?? 1.5,
      maxAttempts: rc.maxAttempts ?? 0,
    } as typeof this.reconnect
    this.sleep = opts.sleep ?? defaultSleep
  }

  getStatus(): BridgeStatus {
    return this.status
  }

  /** 当前已发现的工具（同步读缓存）。 */
  getTools(): ToolDef[] {
    return this.tools
  }

  /** 是否已就绪（有活会话）。 */
  isReady(): boolean {
    return this.status === 'ready' && this.session !== undefined
  }

  private setStatus(s: BridgeStatus): void {
    if (this.status === s) return
    this.status = s
    this.opts.onStatus?.(s)
  }

  /** 首次连接：失败**不抛**（转后台重连），使 dsh 插件在服务未起时也能加载。 */
  async start(): Promise<void> {
    if (this.closed) return
    this.setStatus('connecting')
    try {
      await this.connectOnce()
      this.setStatus('ready')
    } catch (err) {
      this.opts.logger?.warn?.(`[paperpilot] 首连失败（${msgOf(err)}）；转后台重连，不阻塞插件加载`)
      this.setStatus('offline')
      void this.ensureReconnecting().catch(() => {})
    }
  }

  /** 建一次新会话 → 拉工具 → 变化则通知。抛错交由调用方（start/reconnect）处理。 */
  private async connectOnce(): Promise<void> {
    const session = await this.opts.sessionFactory()
    session.onClose = () => {
      // 被动断线（如 SSE 流关闭）：若未主动关闭，触发后台重连。
      if (!this.closed && this.session === session) {
        this.opts.logger?.warn?.('[paperpilot] 会话传输关闭；触发后台重连')
        this.setStatus('reconnecting')
        void this.ensureReconnecting().catch(() => {})
      }
    }
    this.session = session
    const { tools } = await session.listTools()
    const norm = tools.map(t => ({ name: t.name, description: t.description, inputSchema: t.inputSchema }))
    const changed = JSON.stringify(norm.map(t => t.name)) !== JSON.stringify(this.tools.map(t => t.name))
    this.tools = norm
    if (changed) this.opts.onTools?.(norm)
  }

  /** 单飞重连：拆旧会话 → 退避重连直到成功/关闭/超次数。 */
  private ensureReconnecting(): Promise<void> {
    if (this.reconnecting) return this.reconnecting
    const run = async (): Promise<void> => {
      await this.teardown()
      const { initialDelayMs, maxDelayMs, growFactor, maxAttempts } = this.reconnect
      let delay = initialDelayMs
      let attempt = 0
      while (!this.closed) {
        attempt++
        try {
          await this.connectOnce()
          this.setStatus('ready')
          this.opts.logger?.info?.(`[paperpilot] 重连成功（第 ${attempt} 次尝试，新 session）`)
          return
        } catch (err) {
          if (maxAttempts > 0 && attempt >= maxAttempts) {
            this.setStatus('offline')
            throw err
          }
          this.setStatus('reconnecting')
          this.opts.logger?.warn?.(`[paperpilot] 重连第 ${attempt} 次失败（${msgOf(err)}）；${delay}ms 后再试`)
          await this.sleep(delay)
          delay = Math.min(Math.round(delay * growFactor), maxDelayMs)
        }
      }
    }
    this.reconnecting = run().finally(() => {
      this.reconnecting = undefined
    })
    return this.reconnecting
  }

  /** 确保有活会话；没有则重连（含首次惰性连接）。 */
  private async ensureReady(): Promise<void> {
    if (this.session && this.status === 'ready') return
    await this.ensureReconnecting()
  }

  /**
   * 调工具（自愈）：遇 session 失效 / 连不上 → 重连（重新 initialize 拿新 session）→ **重试一次**。
   * 重连仍失败 → 抛**可读的离线错误**（断联显式信号，不再是静默 404）。
   *
   * 注意 ensureReady 也在 try 内：首连/重连本身耗尽时，抛出的也必须是可读离线错误
   * （裸 "Session not found" 对调用方没有任何可操作性）。
   */
  async callTool(name: string, args: unknown, signal?: AbortSignal): Promise<unknown> {
    if (this.closed) throw new Error('[paperpilot] 桥已关闭')
    try {
      await this.ensureReady()
      return await this.session!.callTool(name, args, signal)
    } catch (err) {
      if (signal?.aborted) throw err
      if (!isSessionInvalidError(err) && !isConnectionError(err)) throw err
      this.opts.logger?.warn?.(`[paperpilot] 调用 ${name} 遇 ${classify(err)}（${msgOf(err)}）→ 重连后重试一次`)
      try {
        await this.ensureReconnecting()
        await this.ensureReady()
      } catch (reconnectErr) {
        throw new Error(
          `[paperpilot] PaperPilot 服务离线：调用 ${name} 时连接失效且重连未成功（${msgOf(reconnectErr)}）。`
          + '请确认 launcher（paperpilot ai / paperpilot mcp）在跑；工具仍在列表但暂不可用。',
        )
      }
      return await this.session!.callTool(name, args, signal)
    }
  }

  /** 主动刷新工具表（如重连后想强制对齐）。 */
  async listTools(): Promise<ToolDef[]> {
    await this.ensureReady()
    const { tools } = await this.session!.listTools()
    this.tools = tools.map(t => ({ name: t.name, description: t.description, inputSchema: t.inputSchema }))
    return this.tools
  }

  private async teardown(): Promise<void> {
    const s = this.session
    this.session = undefined
    if (!s) return
    try {
      s.onClose = undefined
    } catch { /* ignore */ }
    try {
      await s.close()
    } catch (err) {
      this.opts.logger?.info?.(`[paperpilot] 关旧会话时出错（忽略）：${msgOf(err)}`)
    }
  }

  /** 主动关闭（插件 teardown）：停止后台重连并关会话。 */
  async close(): Promise<void> {
    this.closed = true
    await this.teardown()
    this.setStatus('offline')
  }
}

function msgOf(err: unknown): string {
  const e = err as { message?: unknown } | null
  return String(e?.message ?? err ?? 'unknown')
}

function classify(err: unknown): string {
  if (isSessionInvalidError(err)) return 'session 失效'
  if (isConnectionError(err)) return '连接失败'
  return '错误'
}
