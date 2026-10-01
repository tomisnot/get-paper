/**
 * 自愈 MCP 桥（`MechaMcpBridge`）单测 —— **本资产自己的桥单测**（零 SDK、零 dsh 依赖）。
 *
 * 跑：`node --test mecha/dsh-panel/mcp-bridge.test.ts`
 * （Node ≥ 22.6 原生剥类型，**不需要任何 npm 依赖**）
 *
 * 蓝本 = Get Paper 的 `dsh/test/mcp-bridge.test.ts`（那套又移植自 EL 的验收套件）；
 * 移植时只换类名与相对路径。**两处是本资产特有的、蓝本里没有的**：
 *
 *  1. ⭐ **资产参数必须真的生效**（R17）：`offlineHint` 与 `logLabel` 都是给项目填的参数，
 *     "声明了却没人读"就是**死缝** ⇒ 各配一条"换掉它、结果就变"的用例。
 *  2. `offlineHint` **两个方向都覆盖**：默认（中性句，资产零项目字面量）+ 显式传入。
 *     ⚠ 默认句被**逐字钉住**（不是"只要不抛就行"）——否则"把默认句删空"也能绿（R8/R11）。
 *
 * ## 头号验收（404 自愈）**怎么才会红**（R7：只绿不红的守卫等于没有）
 *
 * 用例「头号验收」断言三件事：① 旧会话被拆（`stale.closed === true`）；
 * ② 新会话被真的用来重试（`fresh.calls.length === 1`）；③ 回执等于新会话的返回值。
 * ⇒ 它红的条件是**具体的、确定的**：删掉 `callTool` 里的重连重试、或让
 * `isSessionInvalidError` 不再把 HTTP 404 认成"会话失效"（⇒ 走"业务错误原样抛"支），
 * 或让重连复用旧 session（⇒ `fresh.calls.length` 恒 0）。
 * 本用例**不用概率性手段**（不靠时序、不靠真实网络、`sleep: noSleep`）⇒ 确定性。
 *
 * ⭐ **红证已实测（R7/R16：运行时补丁，不改仓内文件）**：把 `mcp-bridge.ts` 复制到临时目录，
 * 在 `isSessionInvalidError` 首行插一句 `return false`（并自证"文件内容确实变了"），
 * 再对那份副本跑本文件 ⇒ **4 条红**（头号验收 + 重连耗尽 + offlineHint + 鸭子类型判别），
 * 其余 7 条绿。⇒ 这条用例**真的抓得住"自愈没了"**，不是哑守卫。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  MechaMcpBridge,
  isConnectionError,
  isSessionInvalidError,
  type McpSession,
  type SessionFactory,
  type ToolDef,
} from './mcp-bridge.ts'

const noSleep = (): Promise<void> => Promise.resolve()

/** 断言里用的不可达端点（RFC 5737 测试网段 + 端口 1；**不是**任何项目的默认端口）。 */
const UNREACHABLE = 'http://192.0.2.1:1/mcp'

/** 可编程的假会话：按队列返回结果/抛错；记录 callTool 历史。
 *  connectFails=true 时 listTools 也抛（模拟"服务没起来"，首连/重连都失败）。 */
class FakeSession {
  onClose?: () => void
  calls: Array<{ name: string; args: unknown }> = []
  closed = false
  connectFails = false
  sessionId: string
  behavior: () => Promise<unknown>

  constructor(sessionId: string,
              opts: { behavior?: () => Promise<unknown>; connectFails?: boolean } = {}) {
    this.sessionId = sessionId
    // ⚠ `behavior` 是**必填语义**：漏给时默认抛错（而不是 `undefined`），
    //    否则 `this.behavior()` 会以 TypeError 冒充"调用失败"，把用例测到别处去。
    this.behavior = opts.behavior ?? (async () => { throw sessionNotFound() })
    this.connectFails = opts.connectFails ?? false
  }

  async listTools(): Promise<{ tools: ToolDef[] }> {
    if (this.connectFails) throw sessionNotFound()
    return { tools: [{ name: 'read_digest', description: 'd', inputSchema: {} }] }
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

/**
 * 造一台"**连得上、后来调不动、且再也连不上**"的桥（自愈耗尽 ⇒ 离线错误那一支）。
 *
 * ⚠ 为什么必须这么造：桥的离线错误（`服务离线…` + `offlineHint`）要求
 * **"调用失败 → 自愈 → 重连也失败"** 这条链走完：
 *  - 若重连成功、只是重试那一次又失败 ⇒ 补丁里的重试再抛 ⇒ 也是**裸错误**
 *    （它已经在 `catch` 块里，不再被同一个 `catch` 接住）；
 *  - 若**首连就失败**：2026-10-01 起 `ensureReady()` 已在 `try` **之内**，
 *    所以同样会走到离线错误那一支 —— 那条路径由
 *    「首连失败之后再调用，也抛可读离线错误」这个用例单独钉住。
 * ⇒ 本函数专造**第一种**形状：**首连成功**（`start()` ready）→ **调用失败**
 *   → **重连也失败**。
 * ⚠ 两个坑都踩过：① 恒返回死会话 ⇒ 连 `start()` 都过不去；
 *   ② 会话一直健康 ⇒ 调用直接成功、根本进不了自愈。
 */
function bridgeThatHealsExhausted(opts: { offlineHint?: string; logLabel?: string } = {}): MechaMcpBridge {
  // 首连成功，但**第一次调用就抛 404**（模拟"服务在两次操作之间重启了"）。
  const staleOnCall = new FakeSession('stale', { behavior: async () => { throw sessionNotFound() } })
  let n = 0
  return new MechaMcpBridge({
    sessionFactory: async () => {
      n++
      // 第 1 次：能连上（start() 变 ready）；此后一律连不上（重连必失败）
      return (n === 1
        ? staleOnCall
        : new FakeSession('dead', { connectFails: true })) as unknown as McpSession
    },
    sleep: noSleep,
    ...(opts.offlineHint !== undefined ? { offlineHint: opts.offlineHint } : {}),
    ...(opts.logLabel !== undefined ? { logLabel: opts.logLabel } : {}),
    reconnect: { initialDelayMs: 0, maxDelayMs: 0, maxAttempts: 1 },
  })
}

test('首连成功 → ready，工具被发现', async () => {
  const s = new FakeSession('s1', { behavior: async () => ({ ok: true }) })
  const seen: string[][] = []
  const bridge = new MechaMcpBridge({
    sessionFactory: sessionFactory([s]),
    sleep: noSleep,
    onTools: tools => seen.push(tools.map(t => t.name)),
  })
  await bridge.start()
  assert.equal(bridge.getStatus(), 'ready')
  assert.deepEqual(seen[0], ['read_digest'])
  assert.equal(bridge.isReady(), true)
})

test('头号验收：服务重启 → 旧 session 404 → 自愈重连（新 session）→ 恢复', async () => {
  // ⚠ 必须用**块体** `{ throw … }`：`async () => sessionNotFound()` 是**表达式体**，
  //    它 `return` 那个 Error ⇒ `await` **resolve** 成 Error 值，异常根本到不了桥那里
  //    （于是"没有重试"看着像桥坏了，其实是本用例自己没抛）。这条真踩过一次。
  const stale = new FakeSession('old', { behavior: async () => { throw sessionNotFound() } })
  const fresh = new FakeSession('new', { behavior: async () => ({ ok: true, content: [] }) })
  const bridge = new MechaMcpBridge({
    sessionFactory: sessionFactory([stale, fresh]),
    sleep: noSleep,
  })
  await bridge.start()
  assert.equal(bridge.getStatus(), 'ready')

  const result = await bridge.callTool('read_digest', { date: '2026-09-20' })
  assert.deepEqual(result, { ok: true, content: [] })
  // 旧会话被拆掉、新会话被用来重试（两句话各自能红，见文件头"怎么才会红"）
  assert.equal(stale.closed, true)
  assert.equal(fresh.calls.length, 1)
  assert.equal(bridge.getStatus(), 'ready')
})

test('连接失败（ECONNREFUSED）也走自愈重连', async () => {
  const dead = new FakeSession('dead', { behavior: async () => {
    throw Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:1'), { code: 'ECONNREFUSED' })
  } })
  const alive = new FakeSession('alive', { behavior: async () => ({ ok: true }) })
  const bridge = new MechaMcpBridge({
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
      const dead = new FakeSession('dead', { behavior: async () => ({ ok: true }) })
      dead.connectFails = true   // 首连就失败（服务没起来）
      return dead as unknown as McpSession
    }
    return new FakeSession('alive', { behavior: async () => ({ ok: true }) }) as unknown as McpSession
  }
  const statuses: string[] = []
  const bridge = new MechaMcpBridge({
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

test('⭐ 首连失败之后再调用，也抛**可读的离线错误**（不是裸 404）', async () => {
  // ⚠ 这条钉的是一处**曾被移植丢掉的修复**（2026-10-01 补）：`callTool` 原先把
  //   `await this.ensureReady()` 放在 `try` **之外** ⇒ 首连失败（无 session、status=offline）
  //   之后再调用，`ensureReconnecting()` 直接抛 ⇒ **裸错误冒出去**，`offlineHint` 那支
  //   永远跑不到。两个消费者（GP 的 DESIGN 明文、EL 的判据）都要求这里给可读错误。
  // ⚠ 能红证据（确定性，R7）：把 `ensureReady()` 挪回 `try` 之外 ⇒ 本用例**必红**
  //   （消息退化成 `Session not found`，两条断言都失败）。**已实测**（见提交说明）。
  const dead = new FakeSession('dead', { connectFails: true })
  const bridge = new MechaMcpBridge({
    sessionFactory: sessionFactory([dead]),
    sleep: noSleep,
    offlineHint: '请运行 launcher.py 重新启动权威。',
    reconnect: { initialDelayMs: 0, maxDelayMs: 0, maxAttempts: 1 },
  })
  await bridge.start()                            // ⭐ 不抛
  assert.equal(bridge.getStatus(), 'offline')     // 自证前置条件：真的没连上
  await assert.rejects(
    () => bridge.callTool('read_digest', {}),
    (err: Error) => {
      assert.match(err.message, /服务离线/)          // 不是静默 404
      assert.ok(err.message.includes('请运行 launcher.py 重新启动权威。'),
        `显式 offlineHint 没进消息：${err.message}`)
      assert.ok(err.message.includes('read_digest'),
        `离线消息里没有调用名：${err.message}`)
      return true
    },
  )
})

test('⭐ 非连接类错误不被冒充成"服务离线"（别把 bug 伪装成域失败）', async () => {
  // `ensureReady()` 移入 `try` 之后，它抛出的错也会进那个 catch ⇒ 必须确认
  // **只有连接/会话类失败才被转成离线错误**：重连里冒出的编程错（如 TypeError）
  // 若被写成"服务离线…"，就是在**把 bug 伪装成域失败**（本仓明确不许：会让人去查网络）。
  // ⚠ 实测结论：此时错误**原样**冒到调用方（不掺 `服务离线`/`offlineHint`）。
  // 第 1 次：连得上，但**调用就抛会话失效**（这才触发自愈 → 去重连）
  const broken = new FakeSession('broken', { behavior: async () => { throw sessionNotFound() } })
  let n = 0
  const bridge = new MechaMcpBridge({
    sessionFactory: async () => {
      n++
      if (n === 1) return broken as unknown as McpSession
      // 第 2 次起：重连路径里冒**非连接类**错误
      const bad = new FakeSession('bad', { behavior: async () => ({ ok: true }) })
      bad.listTools = async () => { throw new TypeError('undefined is not a function') }
      return bad as unknown as McpSession
    },
    sleep: noSleep,
    offlineHint: '不该出现的项目指引',
    reconnect: { initialDelayMs: 0, maxDelayMs: 0, maxAttempts: 1 },
  })
  await bridge.start()
  await assert.rejects(
    () => bridge.callTool('read_digest', {}),
    (err: Error) => {
      assert.match(err.message, /undefined is not a function/)   // 原样透出
      assert.ok(!err.message.includes('服务离线'),
        `编程错被伪装成域失败：${err.message}`)
      assert.ok(!err.message.includes('不该出现的项目指引'),
        `编程错被塞进了 offlineHint：${err.message}`)
      return true
    },
  )
})

test('重连耗尽 → 抛可读的离线错误（默认 offlineHint，且消息含调用名）', async () => {
  const bridge = bridgeThatHealsExhausted()
  await bridge.start()
  assert.equal(bridge.getStatus(), 'ready')   // 自证前置条件：真的是"连上了"
  await assert.rejects(
    () => bridge.callTool('read_digest', {}),
    (err: Error) => {
      // (a) 默认 offlineHint **逐字钉住**（资产的中性句，零项目字面量）
      assert.ok(
        err.message.includes('请确认软件已启动；工具仍在列表但暂不可用。'),
        `默认 offlineHint 没进消息：${err.message}`,
      )
      // (b) 可读且可归因到**是哪一次调用**（R4：错误消息是 LLM 的 UI）
      assert.ok(err.message.includes('read_digest'),
        `离线消息里没有调用名：${err.message}`)
      assert.match(err.message, /服务离线/)
      return true
    },
  )
})

test('⭐ offlineHint 是**真参数**（R17：换掉它、结果就变）', async () => {
  const bridge = bridgeThatHealsExhausted({
    offlineHint: '请运行 energy-level/launcher.py 重新启动权威（本项目专属指引）。',
  })
  await bridge.start()
  await assert.rejects(
    () => bridge.callTool('set_config', {}),
    (err: Error) => {
      assert.ok(err.message.includes('energy-level/launcher.py'),
        `显式 offlineHint 没进消息：${err.message}`)
      // 反面：默认那句**不许**再出现（参数真的**替换**了它，不是叠加）
      assert.ok(!err.message.includes('请确认软件已启动'),
        `默认句与显式值同时出现 ⇒ 参数没替换默认：${err.message}`)
      return true
    },
  )
})

test('⭐ logLabel 是**真参数**（R17：换掉它、结果就变）', async () => {
  // (a) 不给 logLabel ⇒ 资产自己的中性前缀（零项目字面量）
  const defaultLogs: string[] = []
  const a = new MechaMcpBridge({
    sessionFactory: async () => { throw new Error('connect ECONNREFUSED') },
    sleep: noSleep,
    reconnect: { maxAttempts: 1, initialDelayMs: 0, maxDelayMs: 0 },
    logger: { warn: (m: string) => defaultLogs.push(m) },
  })
  await a.start()
  assert.ok(defaultLogs.some(m => m.startsWith('[mcp-bridge]')),
    `logLabel 缺省时该用 [mcp-bridge] 前缀：${JSON.stringify(defaultLogs)}`)
  await a.close()

  // (b) 给了 logLabel ⇒ 该串真的出现在日志里
  const customLogs: string[] = []
  const b = new MechaMcpBridge({
    sessionFactory: async () => { throw new Error('connect ECONNREFUSED') },
    sleep: noSleep,
    logLabel: 'xyz',
    reconnect: { maxAttempts: 1, initialDelayMs: 0, maxDelayMs: 0 },
    logger: { warn: (m: string) => customLogs.push(m) },
  })
  await b.start()
  assert.ok(customLogs.some(m => m.includes('[xyz]')),
    `显式 logLabel 没进日志：${JSON.stringify(customLogs)}`)
  assert.ok(!customLogs.some(m => m.includes('[mcp-bridge]')),
    `缺省前缀不该再出现（参数应**替换**它）：${JSON.stringify(customLogs)}`)
  await b.close()
})

test('close() 后再调 ⇒ 抛"桥已关闭"（不是静默空转）', async () => {
  const s = new FakeSession('s1', { behavior: async () => ({ ok: true }) })
  const bridge = new MechaMcpBridge({
    sessionFactory: sessionFactory([s]),
    sleep: noSleep,
    logLabel: 'xyz',   // 顺带钉住：关闭错误也带同一前缀
  })
  await bridge.start()
  await bridge.close()
  assert.equal(s.closed, true)
  await assert.rejects(() => bridge.callTool('read_digest', {}), /桥已关闭/)
  await assert.rejects(() => bridge.callTool('read_digest', {}), /\[xyz\]/)
})

test('业务错误（非连接类）原样抛出，不触发重连', async () => {
  const s = new FakeSession('s', { behavior: async () => {
    throw new Error('论文不存在')
  } })
  const bridge = new MechaMcpBridge({
    sessionFactory: sessionFactory([s]),
    sleep: noSleep,
  })
  await bridge.start()
  await assert.rejects(() => bridge.callTool('read_paper', { arxiv_id: 'x' }), /论文不存在/)
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

test('⚠ 反面语料：真网络失败（不可达端点）下 start() 也不抛，且不静默', async () => {
  // 分工：上面各例用**假会话**精确测状态机；这一例让 `fetch` 真的失败，
  // 钉住一条真实性质：**端点不可达时 `start()` 仍然不抛**（插件照常加载）——
  // 这正是 dsh 插件不能因软件没起来而加载失败的原因。
  //
  // ⚠ **本用例刻意不在这里断言 `offlineHint`**：那句只出现在"**已连上、再调用时失败**"
  //   的那一支（`callTool` 的 catch 内重连耗尽）。首连就失败时，
  //   `ensureReady()` 在 `try` **之外**抛 ⇒ 裸网络错原样出来。
  //   这是**实测出来的语义**，不是猜测——不写进来的话，下一个人会误以为这里该有指引。
  // ⚠ 地址取 TEST-NET-1 + 端口 1（RFC 5737）：不依赖任何真实服务，零项目字面量。
  const statuses: string[] = []
  const bridge = new MechaMcpBridge({
    sessionFactory: async () => {
      const res = await fetch(UNREACHABLE)     // 拒绝连接 ⇒ 这里抛 TypeError(fetch failed)
      const session = res as unknown as McpSession
      await session.listTools()                // 自证：真把结果当会话用了（否则 fetch 一 resolve 就"成功"）
      return session
    },
    sleep: noSleep,
    reconnect: { initialDelayMs: 0, maxDelayMs: 0, maxAttempts: 1 },
    logLabel: 'xyz',
    logger: {},   // 刻意给空 logger：**不许**因为日志接口缺失而崩
    onStatus: s => statuses.push(s),
  })
  await bridge.start()   // ⭐ 不抛
  assert.equal(bridge.getStatus(), 'offline')
  // 自证真的走到了网络失败（不是"解析成空会话"那种退化输入 ⇒ 恒定观测量）
  assert.ok(statuses.includes('offline'),
    `没有观察到 offline 迁移 ⇒ 这条可能没真的走网络：${JSON.stringify(statuses)}`)
  await bridge.close()
})
