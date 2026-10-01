/**
 * 自愈 MCP 桥单测（移植自 re0-mecha-dsh 的验收套件，零 SDK/零 dsh 依赖）。
 *
 * 头号验收：服务重启 → 旧 session 404 → 自愈重连（新 session）→ 恢复。
 * 对照：不重连的 stock 桥在此场景永久卡死（本测试用假 session 精确复现）。
 *
 * ⚠ **2026-10-01：被测对象换成资产版**（`MechaMcpBridge` @ `../src/panel/mcp-bridge.ts`，
 * 逐字复制自 `mecha/dsh-panel/`）。本文件保留自建版时代的**同一套断言**，用作"资产版与
 * 自建版**行为等价**"的实证（R12：不为了让测试过而放宽断言）。
 *
 * 两处**必要**的适配（其余断言逐字未动）：
 *  1. 构造一律经 `gpBridgeOptions(...)` —— 项目值（`logLabel` / `offlineHint`）现在由
 *     `gp-params.ts` 提供（自建版把这些写死在桥里）⇒ 顺便钉住"GP 的参数真有消费者"（R17）；
 *  2. 「重连耗尽」那条的期望串：报文的项目名从**写死**改成 `logLabel` 参数
 *     （`PaperPilot 服务离线` → `[paperpilot] 服务离线`）⇒ 期望串跟着换成前缀形式，
 *     **断言的实质不变**（可读的离线文案 + 指明 launcher 的指引）。
 */
import assert from 'node:assert/strict'
import {
  MechaMcpBridge,
  isConnectionError,
  isSessionInvalidError,
  type McpSession,
  type SessionFactory,
  type ToolDef,
} from '../src/panel/mcp-bridge.ts'
import { gpBridgeOptions } from '../src/host/gp-hub.ts'
import { GP_BRIDGE } from '../src/gp-params.ts'
import { test } from './harness.ts'

const noSleep = (): Promise<void> => Promise.resolve()

/** 可编程的假会话：按队列返回结果/抛错；记录 callTool 历史。
 *  connectFails=true 时 listTools 也抛（模拟"服务没起来"，首连/重连都失败）。 */
class FakeSession {
  onClose?: () => void
  calls: Array<{ name: string; args: unknown }> = []
  closed = false
  connectFails = false
  sessionId: string
  behavior: () => Promise<unknown>

  constructor(sessionId: string, behavior: () => Promise<unknown>) {
    this.sessionId = sessionId
    this.behavior = behavior
  }

  async listTools(): Promise<{ tools: ToolDef[] }> {
    if (this.connectFails) throw sessionNotFound()
    return { tools: [{ name: 'get_digest', description: 'd', inputSchema: {} }] }
  }

  async callTool(name: string, args: unknown): Promise<unknown> {
    this.calls.push({ name, args })
    return this.behavior()
  }

  async close(): Promise<void> {
    this.closed = true
  }
}

function sessionNotFound(): Error & { code?: number } {
  const err = new Error('Session not found') as Error & { code?: number }
  err.code = 404
  return err
}

function sessionFactory(sessions: FakeSession[]): SessionFactory {
  let i = 0
  return async () => {
    const s = sessions[i] ?? sessions[sessions.length - 1]
    i++
    return s as unknown as McpSession
  }
}

/** 造桥：一律经 GP 的参数家注入项目值（与生产接线同一条路）。 */
function makeBridge(
  sessionFactory: SessionFactory,
  overrides: Parameters<typeof gpBridgeOptions>[1] = {},
): MechaMcpBridge {
  return new MechaMcpBridge(gpBridgeOptions(sessionFactory, overrides))
}

test('首连成功 → ready，工具被发现', async () => {
  const s = new FakeSession('s1', async () => ({ ok: true }))
  const seen: string[][] = []
  const bridge = makeBridge(sessionFactory([s]), {
    sleep: noSleep,
    onTools: tools => seen.push(tools.map(t => t.name)),
  })
  await bridge.start()
  assert.equal(bridge.getStatus(), 'ready')
  assert.deepEqual(seen[0], ['get_digest'])
  assert.equal(bridge.isReady(), true)
})

test('头号验收：服务重启 → 旧 session 404 → 自愈重连（新 session）→ 恢复', async () => {
  const stale = new FakeSession('old', async () => {
    const err = new Error('Session not found') as Error & { code?: number }
    err.code = 404
    throw err
  })
  const fresh = new FakeSession('new', async () => ({ ok: true, content: [] }))
  const bridge = makeBridge(sessionFactory([stale, fresh]), { sleep: noSleep })
  await bridge.start()
  assert.equal(bridge.getStatus(), 'ready')

  const result = await bridge.callTool('get_digest', { date: '2026-09-20' })
  assert.deepEqual(result, { ok: true, content: [] })
  // 旧会话被拆掉、新会话被用来重试
  assert.equal(stale.closed, true)
  assert.equal(fresh.calls.length, 1)
  assert.equal(bridge.getStatus(), 'ready')
})

test('连接失败（ECONNREFUSED）也走自愈重连', async () => {
  const dead = new FakeSession('dead', async () => {
    throw Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:8765'), { code: 'ECONNREFUSED' })
  })
  const alive = new FakeSession('alive', async () => ({ ok: true }))
  const bridge = makeBridge(sessionFactory([dead, alive]), { sleep: noSleep })
  await bridge.start()
  const out = await bridge.callTool('list_topics', {})
  assert.deepEqual(out, { ok: true })
})

test('首连失败不抛（转后台重连），插件照常加载', async () => {
  let n = 0
  const factory: SessionFactory = async () => {
    n++
    if (n === 1) {
      const dead = new FakeSession('dead', async () => ({ ok: true }))
      dead.connectFails = true   // 首连就失败（服务没起来）
      return dead as unknown as McpSession
    }
    return new FakeSession('alive', async () => ({ ok: true })) as unknown as McpSession
  }
  const statuses: string[] = []
  const bridge = makeBridge(factory, {
    sleep: noSleep,
    onStatus: s => statuses.push(s),
  })
  await bridge.start()   // 不抛
  assert.equal(bridge.getStatus(), 'offline')   // 首连失败即 offline（后台重连中）
  await new Promise(r => setTimeout(r, 20))     // 等后台重连成功
  assert.equal(bridge.getStatus(), 'ready')
  assert.ok(statuses.includes('offline'))
})

test('重连耗尽 → 抛可读的离线错误（不再是静默 404）', async () => {
  const dead = new FakeSession('dead', async () => sessionNotFound())
  dead.connectFails = true
  const bridge = makeBridge(sessionFactory([dead]), {
    sleep: noSleep,
    reconnect: { initialDelayMs: 0, maxDelayMs: 0, maxAttempts: 1 },
  })
  await bridge.start()
  await assert.rejects(
    () => bridge.callTool('get_digest', {}),
    /\[paperpilot\] 服务离线.*launcher/,
  )
})

test('业务错误（非连接类）原样抛出，不触发重连', async () => {
  const s = new FakeSession('s', async () => {
    throw new Error('论文不存在')
  })
  const bridge = makeBridge(sessionFactory([s]), { sleep: noSleep })
  await bridge.start()
  await assert.rejects(() => bridge.callTool('get_paper', { arxiv_id: 'x' }), /论文不存在/)
  assert.equal(s.calls.length, 1)  // 没有重试
})

test('鸭子类型判别：session 失效 vs 连接失败', () => {
  assert.equal(isSessionInvalidError(Object.assign(new Error('Session not found'), { code: 404 })), true)
  assert.equal(isSessionInvalidError(new Error('invalid session id')), true)
  assert.equal(isSessionInvalidError(new Error('论文不存在')), false)
  assert.equal(isConnectionError(Object.assign(new Error('x'), { code: 'ECONNRESET' })), true)
  assert.equal(isConnectionError(new Error('fetch failed')), true)
  assert.equal(isConnectionError(new Error('论文不存在')), false)
})

test('⭐ 重连路径里的**编程错**不许被冒充成"服务离线"（原样透出）', async () => {
  // 为什么钉这条：`ensureReady()` 移入 try 之后，"重连路径里的 bug"也会落进同一个 catch。
  // 若一律包装成「服务离线…+offlineHint」，**bug 就被伪装成网络故障**，人/AI 会去查网络，
  // 而真因在代码里（这是资产 2026-10-01 补的第二条守卫，见 `panel/mcp-bridge.ts:265-271`）。
  // 形状：首连成功 → 一次调用触发自愈 → **重连本身**抛 TypeError（不是连接类失败）。
  const stale = new FakeSession('stale', async () => { throw sessionNotFound() })
  let n = 0
  const factory: SessionFactory = async () => {
    n++
    if (n === 1) return stale as unknown as McpSession
    throw new TypeError('reconnect 路径里的编程错（TypeError）')
  }
  const bridge = makeBridge(factory, {
    sleep: noSleep,
    reconnect: { initialDelayMs: 0, maxDelayMs: 0, maxAttempts: 1 },
  })
  await bridge.start()
  assert.equal(bridge.getStatus(), 'ready')   // 自证前置：真的走到"调用失败 → 重连"那一支

  await assert.rejects(
    () => bridge.callTool('get_digest', {}),
    (err: Error) => {
      // (a) 原错误**类型与消息**原样透出（不许被换成一个通用 Error）
      assert.ok(err instanceof TypeError, `原错误类型没透出：${err?.constructor?.name}`)
      assert.match(err.message, /编程错/)
      // (b) 反面：不许出现"服务离线"字样，也不许混进项目的 offlineHint
      assert.ok(!err.message.includes('服务离线'), `编程错被冒充成服务离线：${err.message}`)
      assert.ok(!err.message.includes(GP_BRIDGE.offlineHint),
        `offlineHint 混进了编程错：${err.message}`)
      return true
    },
  )
})
