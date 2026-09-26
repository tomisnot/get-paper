/**
 * 自愈 MCP 桥单测（移植自 re0-mecha-dsh 的验收套件，零 SDK/零 dsh 依赖）。
 *
 * 头号验收：服务重启 → 旧 session 404 → 自愈重连（新 session）→ 恢复。
 * 对照：不重连的 stock 桥在此场景永久卡死（本测试用假 session 精确复现）。
 */
import assert from 'node:assert/strict'
import {
  PaperPilotMcpBridge,
  isConnectionError,
  isSessionInvalidError,
  type McpSession,
  type SessionFactory,
  type ToolDef,
} from '../src/host/mcp-bridge.ts'
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

test('首连成功 → ready，工具被发现', async () => {
  const s = new FakeSession('s1', async () => ({ ok: true }))
  const seen: string[][] = []
  const bridge = new PaperPilotMcpBridge({
    sessionFactory: sessionFactory([s]),
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
  const bridge = new PaperPilotMcpBridge({
    sessionFactory: sessionFactory([stale, fresh]),
    sleep: noSleep,
  })
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
  const bridge = new PaperPilotMcpBridge({
    sessionFactory: sessionFactory([dead, alive]),
    sleep: noSleep,
  })
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
  const bridge = new PaperPilotMcpBridge({
    sessionFactory: factory,
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
  const bridge = new PaperPilotMcpBridge({
    sessionFactory: sessionFactory([dead]),
    sleep: noSleep,
    reconnect: { initialDelayMs: 0, maxDelayMs: 0, maxAttempts: 1 },
  })
  await bridge.start()
  await assert.rejects(
    () => bridge.callTool('get_digest', {}),
    /PaperPilot 服务离线.*launcher/,
  )
})

test('业务错误（非连接类）原样抛出，不触发重连', async () => {
  const s = new FakeSession('s', async () => {
    throw new Error('论文不存在')
  })
  const bridge = new PaperPilotMcpBridge({
    sessionFactory: sessionFactory([s]),
    sleep: noSleep,
  })
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
