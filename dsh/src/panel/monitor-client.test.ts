/**
 * 取数骨架单测（**共享测试**：随参考实现一起被复制）。
 *
 * 跑：`node --test mecha/dsh-panel/monitor-client.test.ts`
 *
 * 钉死四件事：
 *  1. **档位可区分**：基础路由失败 = `offline`；**附加路由失败 = `route`**；地址拿不到
 *     = `address`（后者若混进前者，真因会被"权威离线"吃掉——这正是本骨架要防的）；
 *  2. **失败不冒充成功**：非 2xx / 非 JSON ⇒ `ok:false`，**绝不返回空对象**；
 *  3. **R8 非退化自证**：`isNonDegenerate` 必须**两个方向都能变**（空 ⇒ false、
 *     有数据 ⇒ true），否则它就是个恒定的观测量（R11）；
 *  4. **注入的缝是真的**：喂进去的 `doFetch` **必须真的被调用**，而且**换掉它必须
 *     改变结果**（R16：自证"扰动真的落上了"——不是只看签名长得能注入）。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  assertNever,
  classifyRoute,
  displayTier,
  fetchMonitorBase,
  isNonDegenerate,
  isOriginLike,
  monitorStats,
  panelState,
  readJson,
  statsLine,
} from './monitor-client.ts'

/** fetch 替身：按 path 决定回什么，并**记下每次调用**（判据据此自证注入生效）。 */
function fakeFetch(reply: (path: string) => Response | Error) {
  const calls: string[] = []
  const f = (async (input: any) => {
    const raw = String(input)
    calls.push(raw)
    const path = raw.replace(/^https?:\/\/[^/]+/, '')
    const r = reply(path)
    if (r instanceof Error) throw r
    return r
  }) as unknown as typeof fetch
  return { f, calls }
}

function jsonRes(status: number, payload: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as unknown as Response
}

test('classifyRoute：四基础路由 = basic，其余 = extra', () => {
  for (const p of ['/status', '/activity', '/history', '/config']) {
    assert.equal(classifyRoute(p), 'basic', p)
  }
  assert.equal(classifyRoute('/history?since_seq=0'), 'basic')   // 查询串不改变归属
  assert.equal(classifyRoute('/summary'), 'extra')
  assert.equal(classifyRoute('/runs?limit=50'), 'extra')
})

// ---------------------------------------------------------------- 补 1：注入的缝是真的

test('⭐ fetchMonitorBase：注入的 doFetch **真的被调用**，且**换掉它结果就变**（R16）', async () => {
  // ① 成功的假 fetch：必须被调用，且**调用的正是地址路由**
  const ok = fakeFetch(() => jsonRes(200, { base: 'http://127.0.0.1:41000' }))
  const got = await fetchMonitorBase(ok.f, '/probe/monitor-url')
  assert.equal(ok.calls.length, 1, '注入的 doFetch 一次都没被调用 ⇒ 这个缝是假的')
  assert.equal(ok.calls[0], '/probe/monitor-url', '调用的路径不是传入的那个')
  assert.equal(got.ok, true)
  assert.equal((got as any).data, 'http://127.0.0.1:41000')

  // ② 换一个"永远抛"的假 fetch ⇒ **结果必须变**（否则等于没注入）
  const boom = fakeFetch(() => new Error('ECONNREFUSED'))
  const failed = await fetchMonitorBase(boom.f)
  assert.equal(boom.calls.length, 1, '第二条替身也没被调用 ⇒ 注入没生效')
  assert.equal(failed.ok, false)
  assert.equal((failed as any).failure.tier, 'address')

  // ③ 三条支路各自自证（扰动确实走到了那一支）
  const s503 = fakeFetch(() => jsonRes(503, { error: '监控端点未知' }))
  assert.equal(((await fetchMonitorBase(s503.f)) as any).failure.detail, '地址路由返回 503')

  const badJson = fakeFetch(() => ({
    ok: true, status: 200, json: async () => { throw new Error('bad json') },
  } as unknown as Response))
  assert.match(((await fetchMonitorBase(badJson.f)) as any).failure.detail, /不是 JSON/)

  const junk = fakeFetch(() => jsonRes(200, { base: 'not-a-url' }))
  assert.match(((await fetchMonitorBase(junk.f)) as any).failure.detail, /地址形状不对/)
})

test('⭐ 补 4：地址形状自检（垃圾地址归 address 档，不许一路走到 connect 失败）', async () => {
  assert.equal(isOriginLike('http://127.0.0.1:41000'), true)
  assert.equal(isOriginLike('https://host:1'), true)
  assert.equal(isOriginLike(''), false)
  assert.equal(isOriginLike('not-a-url'), false)
  assert.equal(isOriginLike('null'), false)          // 这就是"相对/空地址"伪装的形状
  assert.equal(isOriginLike(undefined), false)

  for (const bad of ['', 'not-a-url', 'null']) {
    const { f } = fakeFetch(() => jsonRes(200, { base: bad }))
    const r = await fetchMonitorBase(f)
    assert.equal(r.ok, false, `坏形状 ${JSON.stringify(bad)} 必须失败`)
    assert.equal((r as any).failure.tier, 'address')
  }
})

// ---------------------------------------------------------------- 取数

test('⭐ 附加路由失败 ⇒ route 档；基础路由失败 ⇒ offline 档（**必须分开**）', async () => {
  const { f } = fakeFetch((path) => jsonRes(path.startsWith('/summary') ? 500 : 503, {}))
  const extra = await readJson('http://127.0.0.1:1', '/summary', f)
  const basic = await readJson('http://127.0.0.1:1', '/history', f)
  assert.equal(extra.ok, false)
  assert.equal(basic.ok, false)
  assert.equal(extra.ok === false && extra.failure.tier, 'route')
  assert.equal(basic.ok === false && basic.failure.tier, 'offline')
  assert.notEqual((extra as any).failure.tier, (basic as any).failure.tier)
})

test('连不上 / 非 JSON ⇒ ok:false，**不返回空对象冒充成功**', async () => {
  const { f: dead } = fakeFetch(() => new Error('ECONNREFUSED'))
  const d = await readJson('http://127.0.0.1:1', '/status', dead)
  assert.equal(d.ok, false)
  const { f: notJson } = fakeFetch(() => ({
    ok: true, status: 200, json: async () => { throw new Error('bad json') },
  } as unknown as Response))
  const n = await readJson('http://127.0.0.1:1', '/config', notJson)
  assert.equal(n.ok, false)
  assert.equal(n.ok === false && n.failure.tier, 'offline')
})

test('取数成功时如实返回数据（对偶：不能永远失败）', async () => {
  const { f } = fakeFetch(() => jsonRes(200, { groups: { a: {} } }))
  const good = await readJson('http://127.0.0.1:1', '/config', f)
  assert.equal(good.ok, true)
  assert.deepEqual((good as any).data, { groups: { a: {} } })
})

test('displayTier：把 address **显式**塌缩成 offline；route 不塌缩', () => {
  assert.equal(displayTier('address'), 'offline')
  assert.equal(displayTier('offline'), 'offline')
  assert.equal(displayTier('route'), 'route')
})

// ---------------------------------------------------------------- R8 自证

test('⭐ R8 非退化自证：两个方向都能变（空 ⇒ false，有数据 ⇒ true）', () => {
  const empty = monitorStats({ offline: false, history: { events: [] }, config: { groups: {} } })
  const real = monitorStats({
    offline: false,
    history: { events: [{ seq: 1 }, { seq: 2 }] },
    config: { groups: { field: {}, dynamic: {} } },
  })
  const offline = monitorStats({ offline: true, history: { events: [] }, config: {} })

  assert.equal(isNonDegenerate(empty), false, '空表不该被判为"正常"（否则就是假绿）')
  assert.equal(isNonDegenerate(real), true)
  assert.equal(isNonDegenerate(offline), false)

  assert.equal(statsLine(real), '离线=false 事件行=2 配置族=2')
  assert.equal(statsLine(empty), '离线=false 事件行=0 配置族=0')
  assert.notEqual(statsLine(real), statsLine(empty), '自证行必须随数据变化（R11）')
})

// ---------------------------------------------------------------- 补 2：归约

const OK_BASE = { ok: true as const, data: 'http://127.0.0.1:1' }
const okData = (d: unknown) => ({ ok: true as const, data: d })
const fail = (tier: any, detail = 'x') => ({ ok: false as const, failure: { tier, detail } })

test('panelState：五种状态各走一次（顺序：地址 → 基础 → 附加 → 非退化 → ok）', () => {
  // ① 地址拿不到（含"还没有取址结果"）
  assert.equal(panelState({}).kind, 'address')
  assert.equal(panelState({ base: fail('address') }).kind, 'address')

  // ② 基础路由失败 ⇒ offline（**不是** route）
  const off = panelState({ base: OK_BASE, basic: { '/history': fail('offline') } })
  assert.equal(off.kind, 'offline')
  assert.deepEqual((off as any).failed, ['/history'])

  // ③ 附加路由失败 ⇒ route（**不是** offline），且**优先于** empty
  const rt = panelState({
    base: OK_BASE,
    basic: { '/history': okData({ events: [] }) },
    extra: { '/summary': fail('route') },
  })
  assert.equal(rt.kind, 'route')
  assert.deepEqual((rt as any).failed, ['/summary'])

  // ④ ⭐ 连上了、也都 200，但**一点数据都没有** ⇒ empty（**不许当成成功**）
  const em = panelState({
    base: OK_BASE,
    basic: { '/history': okData({ events: [] }), '/config': okData({ groups: {} }) },
  })
  assert.equal(em.kind, 'empty')
  assert.match((em as any).detail, /连上了但没拿到数据/)

  // ⑤ 真拿到东西 ⇒ ok，并带上自证计数
  const okState = panelState({
    base: OK_BASE,
    basic: { '/history': okData({ events: [{ seq: 1 }] }), '/config': okData({ groups: { a: {} } }) },
  })
  assert.equal(okState.kind, 'ok')
  assert.equal((okState as any).stats.events, 1)
  assert.equal((okState as any).stats.configFamilies, 1)
})

test('⭐ 补 2 的能红证据：渲染方漏掉一个 kind ⇒ 兜底**抛**（不是什么都不画）', () => {
  // 模拟渲染方只处理了 ok/offline，漏了 address —— 走到兜底必须**抛**
  const render = (s: { kind: string; detail?: string }) => {
    switch (s.kind) {
      case 'ok': return 'data'
      case 'offline': return `离线：${s.detail}`
      default: return assertNever(s as never)
    }
  }
  assert.equal(render({ kind: 'ok' }), 'data')
  assert.equal(render({ kind: 'offline', detail: 'x' }), '离线：x')
  assert.throws(() => render({ kind: 'empty', detail: '空' }), /未处理的 PanelState/,
    '漏掉的分支必须抛，否则就是"静默空白"')
  assert.throws(() => assertNever({ kind: 'whatever' } as never), /未处理的 PanelState/)
})

test('panelState 是全函数：任何组合都返回一个带 detail/数据的 kind（没有"空"档）', () => {
  const combos = [
    {},
    { base: fail('address') },
    { base: OK_BASE },
    { base: OK_BASE, basic: {} },
    { base: OK_BASE, basic: { '/status': fail('offline') } },
    { base: OK_BASE, basic: { '/status': okData({}) }, extra: { '/runs': fail('route') } },
    { base: OK_BASE, basic: { '/status': okData({}) } },
  ]
  const KINDS = ['ok', 'address', 'offline', 'route', 'empty']
  for (const c of combos) {
    const s = panelState(c as any)
    assert.ok(KINDS.includes(s.kind), `未预期的 kind：${s.kind}`)
    const carries = (s as any).detail !== undefined || (s as any).payloads !== undefined
    assert.ok(carries, `状态 ${s.kind} 既没 detail 也没数据 ⇒ 渲染方只能画空白`)
  }
})
