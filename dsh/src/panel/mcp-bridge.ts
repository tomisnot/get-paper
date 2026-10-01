/**
 * MechaMcpBridge —— 自愈的 MCP 客户端，专治 dsh stock HTTP 桥的"服务重启即永久失联"死区。
 *
 * 背景（见 内部记录（未随仓发布，存档在仓外） §2.3）：dsh 的 `dsh-mcp-client` 只在
 * `transport.onclose` 触发时重连，而 streamable-http 的 POST 失败（服务重启后旧
 * `Mcp-Session-Id` → 404 "Session not found"）**只抛错、不触发 onclose**，SDK 也不用 404
 * 重置 session ⇒ 客户端永久卡死，只能重启 Host。我们 Hub 恰是 stateful HTTP，落进这个死区。
 *
 * 本桥接管连接生命周期：任何一次 list/call 遇到"session 失效 / 连不上"，就 **拆旧连接 →
 * 重新 initialize（拿新 session）→ 重试一次**；并有后台退避重连 + 健康探活 + 状态事件。
 *
 * **本模块零 SDK、零 dsh 依赖**（纯逻辑，可注入 fake session 精确单测）；真正的 SDK 接线在
 * `mcp-session-http.ts`。错误识别用鸭子类型（HTTP 状态码为正、JSON-RPC 码为负，不冲突），
 * 故不需要 import `StreamableHTTPError`。
 *
 * ## ⚠ 类名不是项目参数（别每个项目改一次）
 *
 * **类名 `MechaMcpBridge` 是资产自己的标识，不是品牌位**——本资产就叫 mecha。
 * 项目要自己的名字，改的是**参数**：`BridgeOptions.logLabel`（日志前缀）与 `offlineHint`
 * （断联指引）。⇒ 这样"逐字复制"才成立；否则每抄一次都要动文件内容，比对立刻失效。
 * （已有项目按自己的桥名（如 `PaperPilotMcpBridge`）复制，那是它们的选择，**不是本资产的约定**。）
 *
 * ⚠ **这段纪律的唯一权威在 `README.md`**（D1：一个事实一个家）——本文件只留链接，
 * 复述会分叉。参数清单见 README 的「参数表」，逐字复制的验收见它的「复制约定」。
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
  /**
   * 日志前缀标签（**项目值**，填该项目自己的短名）；日志一律形如 `[<label>] …`。
   * 缺省 `'mcp-bridge'` ⇒ 资产自身零项目字面量。
   */
  logLabel?: string
  /**
   * 断联时给调用方看的那句**可操作指引**（**项目值**）。
   *
   * ⚠ 为什么是参数而不是资产内置：离线要"可读且可操作"，而"怎么把软件重新起来"
   * 是项目知识（谁启动权威、怎么切模式）。资产只保证**失败说得清、不静默**，
   * 不替项目编造启动方式。缺省给一句中性指引。
   */
  offlineHint?: string
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

/** 鸭子类型判"连不上"：node 网络错误码 / fetch failed / 5xx / 请求超时。 */
export function isConnectionError(err: unknown): boolean {
  const e = err as { code?: unknown; message?: unknown; cause?: unknown } | null
  const cause = (e?.cause ?? {}) as { code?: unknown; message?: unknown }
  const parts = [e?.message, e?.code, cause.message, cause.code, err]
  const msg = parts.map(p => String(p ?? '')).join(' ')
  return /ECONNREFUSED|ECONNRESET|ENOTFOUND|EPIPE|EAI_AGAIN|socket hang up|fetch failed|network|terminated|timed out|\b50[0-9]\b/i.test(msg)
}

const defaultSleep = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms))

/**
 * 自愈 MCP 桥。生命周期：`start()` 首连（失败不抛，转后台重连，不阻塞插件加载）；
 * `callTool()`/`listTools()` 遇到 session 失效或连不上时自动重连并重试一次；`close()` 停。
 */
export class MechaMcpBridge {
  private session?: McpSession
  private tools: ToolDef[] = []
  private status: BridgeStatus = 'connecting'
  private closed = false
  /** 单飞：并发的重连共用同一个 in-flight promise（避免重连风暴）。 */
  private reconnecting?: Promise<void>
  private readonly reconnect: Required<Pick<ReconnectOptions, 'initialDelayMs' | 'maxDelayMs' | 'growFactor'>> & { maxAttempts: number }
  private readonly sleep: (ms: number) => Promise<void>
  private readonly opts: BridgeOptions
  /** 派生自 `opts.logLabel`（唯一来源），日志一律经它加前缀 ⇒ 零散字面量不再是手抄物。 */
  private readonly tag: string
  private readonly offlineHint: string

  // 不用 TS 参数属性（constructor(private opts)）：dsh 用 Node 原生 strip-only 加载 .ts，
  // 只擦类型、不生成代码，参数属性会 ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX。显式字段+赋值才可擦除。
  constructor(opts: BridgeOptions) {
    this.opts = opts
    this.tag = `[${opts.logLabel?.trim() || 'mcp-bridge'}]`
    this.offlineHint = opts.offlineHint?.trim()
      || '请确认软件已启动；工具仍在列表但暂不可用。'
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

  /** 首次连接：失败**不抛**（转后台重连），使 dsh 插件在 Hub 未起时也能加载。 */
  async start(): Promise<void> {
    if (this.closed) return
    this.setStatus('connecting')
    try {
      await this.connectOnce()
      this.setStatus('ready')
    } catch (err) {
      this.opts.logger?.warn?.(`${this.tag} 首连失败（${msgOf(err)}）；转后台重连，不阻塞插件加载`)
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
        this.opts.logger?.warn?.(`${this.tag} 会话传输关闭；触发后台重连`)
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
          this.opts.logger?.info?.(`${this.tag} 重连成功（第 ${attempt} 次尝试，新 session）`)
          return
        } catch (err) {
          if (maxAttempts > 0 && attempt >= maxAttempts) {
            this.setStatus('offline')
            throw err
          }
          this.setStatus('reconnecting')
          this.opts.logger?.warn?.(`${this.tag} 重连第 ${attempt} 次失败（${msgOf(err)}）；${delay}ms 后再试`)
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
   * ⚠ **`ensureReady()` 必须在 `try` 之内**（2026-10-01 修，**这是一处被移植时丢掉的修复**）：
   * 它在 try 外时，"首连失败（`start()` 失败 ⇒ offline、无 session）**之后**再调 `callTool`"
   * 会让 `ensureReconnecting()` 直接抛 ⇒ **裸 404 冒到调用方**，下面那句精心写的
   * `服务离线… + offlineHint` **永远跑不到**（消费者的学习成本就从"重连耗尽"变成"看不懂的 404"）。
   * 两个消费者都要求这一条，且 EL 已用判据钉住它
   * （`dsh/test/reconnect.test.ts` 的「Hub 宕机：调用抛**可读离线错误**（非静默 404）」）。
   */
  async callTool(name: string, args: unknown, signal?: AbortSignal): Promise<unknown> {
    if (this.closed) throw new Error(`${this.tag} 桥已关闭`)
    try {
      await this.ensureReady()
      return await this.session!.callTool(name, args, signal)
    } catch (err) {
      if (signal?.aborted) throw err
      if (!isSessionInvalidError(err) && !isConnectionError(err)) throw err
      this.opts.logger?.warn?.(`${this.tag} 调用 ${name} 遇 ${classify(err)}（${msgOf(err)}）→ 重连后重试一次`)
      try {
        await this.ensureReconnecting()
      } catch (reconnectErr) {
        // ⚠ **只有连接/会话类失败才配叫"服务离线"**（2026-10-01 补）：
        // `ensureReady()` 移入 `try` 之后，重连路径里冒出的**编程错**（TypeError 之类）
        // 也会落到这一支；若一律包装成"服务离线…"，就是**把 bug 伪装成域失败**
        // （本仓明确不许，见 commands.invoke 的同款纪律）——会让人去查网络，而真因在代码里。
        if (!isSessionInvalidError(reconnectErr) && !isConnectionError(reconnectErr)) {
          throw reconnectErr
        }
        throw new Error(
          `${this.tag} 服务离线：调用 ${name} 时连接失效且重连未成功（${msgOf(reconnectErr)}）。`
          + this.offlineHint,
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
      this.opts.logger?.info?.(`${this.tag} 关旧会话时出错（忽略）：${msgOf(err)}`)
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
