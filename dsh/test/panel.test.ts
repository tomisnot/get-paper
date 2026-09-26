/**
 * PaperPilot **面板判据**（项目自己那份）。
 *
 * 跑：`node --test test/panel.test.ts`
 * （**零 npm 依赖**：只用 `node:test` / `node:assert` / `node:fs` —— Node ≥ 22.6 原生剥类型）
 *
 * 共享件（`src/panel/*.ts` 与它们的 `.test.ts`）**逐字复制**自
 * `mecha/mecha/dsh-panel/`（提交 `eab7b9e`），**不许改**；项目自己的**反面语料**
 * （"不许回落到历史默认 8080"这类）就放本文件。
 *
 * 钉四件事：
 *  1. **地址不回落**：端口文件缺失/非法 ⇒ 路由 `503` 且失败体**没有 `base`**，且**不许**
 *     回落到任何默认端口（本项目曾有一份静态默认 `http://127.0.0.1:8080/`）。
 *  2. **取址缝是真的**（资产 `fetchMonitorBase(doFetch, routePath)`）：喂假 fetch 真的决定
 *     结果，且**只**打 `PANEL_CONFIG.ROUTE_PATH`；形状不对（空/非 `http(s)://`）也归
 *     `address` 档——不许让垃圾地址一路走到"连不上"被误读成"权威离线"。
 *  3. **面板不空白（R8）**：`renderPanel` 的**每个**状态要么给非空**绝对**地址、要么给
 *     非空**可读**错误；**不存在**"两者都不是"的第三态。⚠ 非退化：成功态必须真的带上
 *     喂进去的地址（否则"两边都空"也会过——那是假绿）。
 *  4. **两跳必须可诊断**：地址拿到了但**没人应答**（Web 没起/已退出）⇒ 不许挂一个注定
 *     空白的 iframe，必须给可读错误。缺地址、缺容器同理（**禁止静默 return**）。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  BASIC_ROUTES,
  DEFAULT_ROUTE_PATH,
  makeMonitorUrlHandler,
  resolveMonitorBase,
} from '../src/panel/monitor-url.ts'
import { PANEL_CONFIG } from '../src/panel/panel-config.ts'
import { assertNever, fetchMonitorBase, isOriginLike } from '../src/panel/monitor-client.ts'
import { probeReachable } from '../src/client/panel-probe.ts'
import { renderPanel, viewPath, type PanelView } from '../src/client/panel-state.ts'

/** 本项目**历史**的静态默认地址（迁移前写在插件配置里）——只作为**反面语料**。 */
const HISTORICAL_DEFAULT = 'http://127.0.0.1:8080'

function fakeRes() {
  const res: any = {
    statusCode: 0,
    headers: {} as Record<string, string>,
    body: '',
    setHeader(k: string, v: string) { res.headers[k] = v },
    end(b: string | Uint8Array) {
      res.body = typeof b === 'string' ? b : Buffer.from(b).toString('utf8')
    },
  }
  return res
}

function jsonRes(status: number, payload: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => payload } as unknown as Response
}

/** 造一个 fetch 替身（资产那份自测同款思路：按 URL 决定回什么）。 */
function fakeFetch(reply: (url: string) => Response | Error, seen?: string[]) {
  return (async (input: any) => {
    const url = String(input)
    seen?.push(url)
    const r = reply(url)
    if (r instanceof Error) throw r
    return r
  }) as unknown as typeof fetch
}

function withTmp(fn: (dir: string) => void): void {
  const dir = mkdtempSync(join(tmpdir(), 'paperpilot-panel-'))
  try {
    fn(dir)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

/** 判定输入的两片假数据（**必须真喂**，否则"两边都空"也过）。 */
const okAddress = (base: string) => ({ ok: true as const, data: base })
const badAddress = (tier: 'address' | 'offline' | 'route', detail: string) =>
  ({ ok: false as const, failure: { tier, detail } })

function judged(input: {
  address: ReturnType<typeof okAddress> | ReturnType<typeof badAddress>
  reachable?: boolean
  view?: PanelView
  hasHost?: boolean
}) {
  return renderPanel({
    address: input.address as never,
    reachable: input.reachable ?? true,
    view: input.view ?? 'monitor',
    hasHost: input.hasHost ?? true,
    routePath: PANEL_CONFIG.ROUTE_PATH,
    portFile: PANEL_CONFIG.PORT_FILE,
  })
}

// ---------------------------------------------------------------- 参数块

test('参数块按项目填好（不是参考实现的中性默认，也不是空）', () => {
  assert.equal(PANEL_CONFIG.PORT_FILE, '.web-port', 'PORT_FILE 必须指向 Web 真正写的那个文件')
  assert.equal(PANEL_CONFIG.ROUTE_PATH, '/paperpilot/monitor-url')
  assert.notEqual(PANEL_CONFIG.ROUTE_PATH, DEFAULT_ROUTE_PATH)
  assert.ok(PANEL_CONFIG.TITLE.length > 0)
  assert.equal(BASIC_ROUTES.length, 4)
})

// ---------------------------------------------------------------- ① 地址不回落

test('端口文件在 ⇒ 路由 200 + {base}（读的正是项目根 .web-port）', () => {
  withTmp((dir) => {
    writeFileSync(join(dir, PANEL_CONFIG.PORT_FILE), '8123', 'utf8')
    assert.equal(resolveMonitorBase({ root: dir, portFile: PANEL_CONFIG.PORT_FILE }),
      'http://127.0.0.1:8123')
    const res = fakeRes()
    makeMonitorUrlHandler({ root: dir, portFile: PANEL_CONFIG.PORT_FILE })({} as any, res)
    assert.equal(res.statusCode, 200)
    assert.deepEqual(JSON.parse(res.body), { base: 'http://127.0.0.1:8123' })
  })
})

test('⭐ 端口文件缺失 ⇒ 503 + 无 base，且**绝不回落到历史默认**', () => {
  withTmp((dir) => {
    const res = fakeRes()
    makeMonitorUrlHandler({ root: dir, portFile: PANEL_CONFIG.PORT_FILE })({} as any, res)
    assert.equal(res.statusCode, 503)
    const body = JSON.parse(res.body)
    assert.equal(body.base, undefined)                       // 不许回半截地址
    assert.match(body.error, /监控端点未知/)
    // 反面语料：不许偷偷用历史默认 —— 契约里能出现地址的地方只有 base，这里已为 undefined；
    // 再钉一次"错误体里也不许出现地址"，防止有人把默认值塞进文案当"提示"。
    assert.ok(!res.body.includes(HISTORICAL_DEFAULT), `失败体不许含默认地址：${res.body}`)
    assert.ok(!/\d{2,5}/.test(body.error), `错误文案里不该出现端口数字：${body.error}`)
  })
})

test('端口文件坏内容（旧式整条 URL / 越界 / 0）⇒ 503（不猜、不截断）', () => {
  withTmp((dir) => {
    for (const bad of [`${HISTORICAL_DEFAULT}/mcp`, '70000', '0']) {
      writeFileSync(join(dir, PANEL_CONFIG.PORT_FILE), bad, 'utf8')
      const res = fakeRes()
      makeMonitorUrlHandler({ root: dir, portFile: PANEL_CONFIG.PORT_FILE })({} as any, res)
      assert.equal(res.statusCode, 503, `坏内容 ${bad} 必须是 503`)
      assert.equal(JSON.parse(res.body).base, undefined)
    }
  })
})

test('端口漂移自愈：文件一改，下一次解析就落在新端口（不缓存）', () => {
  withTmp((dir) => {
    const pf = join(dir, PANEL_CONFIG.PORT_FILE)
    writeFileSync(pf, '8123', 'utf8')
    assert.equal(resolveMonitorBase({ root: dir, portFile: PANEL_CONFIG.PORT_FILE }),
      'http://127.0.0.1:8123')
    writeFileSync(pf, '8180', 'utf8')
    assert.equal(resolveMonitorBase({ root: dir, portFile: PANEL_CONFIG.PORT_FILE }),
      'http://127.0.0.1:8180')
  })
})

// ---------------------------------------------------------------- ② client 取址（走**共享** fetchMonitorBase）

test('fetchMonitorBase：真形状 ⇒ base；只打本项目的 ROUTE_PATH', async () => {
  const seen: string[] = []
  const r = await fetchMonitorBase(
    fakeFetch(() => jsonRes(200, { base: 'http://127.0.0.1:8123' }), seen),
    PANEL_CONFIG.ROUTE_PATH)
  assert.equal(r.ok, true)
  assert.equal(r.ok === true && r.data, 'http://127.0.0.1:8123')
  assert.deepEqual(seen, [PANEL_CONFIG.ROUTE_PATH])          // 单一来源：不是别的项目的路由路径
})

test('⭐ 取址缝是真的（R1）：喂不同 fetch 得到不同结果，且都不回落', async () => {
  // 同一个调用点，替身一换结论就变 ⇒ 缝真的在被使用（不是"声明了没人用"的假缝）
  const ok = await fetchMonitorBase(fakeFetch(() => jsonRes(200, { base: 'http://127.0.0.1:9' })),
    PANEL_CONFIG.ROUTE_PATH)
  const dead = await fetchMonitorBase(fakeFetch(() => new Error('ECONNREFUSED')),
    PANEL_CONFIG.ROUTE_PATH)
  assert.equal(ok.ok, true)
  assert.equal(dead.ok, false)
  assert.notDeepEqual(ok, dead)
})

test('地址失败一律落 address 档：非 2xx / 正文非 JSON / 缺 base / 形状不对 / 连不上', async () => {
  const cases: Array<[string, () => Response | Error]> = [
    ['503', () => jsonRes(503, { error: '监控端点未知' })],
    ['缺 base', () => jsonRes(200, { error: 'oops' })],
    ['base 为空', () => jsonRes(200, { base: '' })],
    ['base 是相对串', () => jsonRes(200, { base: 'monitor' })],
    ['base 是路径', () => jsonRes(200, { base: '/monitor' })],
    ['连不上', () => new Error('ECONNREFUSED')],
  ]
  for (const [label, reply] of cases) {
    const r = await fetchMonitorBase(fakeFetch(reply), PANEL_CONFIG.ROUTE_PATH)
    assert.equal(r.ok, false, `${label} 必须失败`)
    assert.equal(r.ok === false && r.failure.tier, 'address', `${label} 必须落 address 档`)
    assert.ok(r.ok === false && r.failure.detail.length > 0, `${label} 必须给 detail`)
    // 绝不回落到历史默认（反面语料）
    assert.ok(r.ok === false && !r.failure.detail.includes(HISTORICAL_DEFAULT))
  }
})

test('isOriginLike：只认 http(s):// 的源样字符串', () => {
  assert.equal(isOriginLike('http://127.0.0.1:8123'), true)
  assert.equal(isOriginLike('https://example.com'), true)
  assert.equal(isOriginLike('http://127.0.0.1:8123/'), true)
  assert.equal(isOriginLike('127.0.0.1:8123'), false)
  assert.equal(isOriginLike('/monitor'), false)
  assert.equal(isOriginLike(''), false)
  assert.equal(isOriginLike(undefined), false)
  assert.equal(isOriginLike(8123), false)
})

// ---------------------------------------------------------------- 两跳可达性

test('probeReachable：有人应答 ⇒ true；连不上 ⇒ false（**绝不抛**）', async () => {
  const seen: string[] = []
  assert.equal(await probeReachable('http://127.0.0.1:8123', fakeFetch(() => jsonRes(200, {}), seen)), true)
  assert.equal(seen[0], 'http://127.0.0.1:8123/healthz')
  assert.equal(await probeReachable('http://127.0.0.1:8123/', fakeFetch(() => jsonRes(200, {}))), true)
  assert.equal(await probeReachable('http://127.0.0.1:8123', fakeFetch(() => new Error('ECONNREFUSED'))), false)
  // 观测量必须能变（R11）：同一输入换替身 ⇒ 结论翻转
  assert.notEqual(
    await probeReachable('http://127.0.0.1:1', fakeFetch(() => jsonRes(200, {}))),
    await probeReachable('http://127.0.0.1:1', fakeFetch(() => new Error('x'))),
  )
})

// ---------------------------------------------------------------- ③ 面板不空白（R8）

test('地址失败 ⇒ 可读错误文案（含档位与细节、含指路），**不是空 DOM**', () => {
  const r = judged({ address: badAddress('address', '读不到监控端口（.web-port 不存在）') })
  assert.equal(r.kind, 'error')
  if (r.kind !== 'error') return
  assert.ok(r.message.trim().length > 40, `文案太短（等于没说）：${r.message}`)
  assert.match(r.message, /address/)                       // 档位可见
  assert.match(r.message, /读不到监控端口/)                  // 细节透传（真因不被吃掉）
  assert.match(r.message, /\.web-port/)                    // 指路：名字说清楚
  assert.match(r.message, /paperpilot serve|paperpilot ai/) // 指路：怎么救
})

test('⭐ 地址拿到了但没人应答（两跳） ⇒ 也可读（不挂注定空白的 iframe）', () => {
  const r = judged({ address: okAddress('http://127.0.0.1:8123'), reachable: false })
  assert.equal(r.kind, 'error')
  if (r.kind !== 'error') return
  assert.match(r.message, /没人应答/)
  assert.match(r.message, /8123/)
  assert.match(r.message, /\.web-port/)
  assert.ok(r.message.trim().length > 40)
  // R11：可达与否必须给出不同结论（否则这一档是个恒定装饰）
  assert.notDeepEqual(judged({ address: okAddress('http://127.0.0.1:8123'), reachable: true }), r)
})

test('右栏容器缺失 ⇒ 也可读（不是静默 return）', () => {
  const r = judged({ address: okAddress('http://127.0.0.1:8123'), hasHost: false })
  assert.equal(r.kind, 'error')
  if (r.kind !== 'error') return
  assert.match(r.message, /data-rightbar-col/)
  assert.ok(r.message.trim().length > 20)
})

test('错误态都该重试（Web 比 dsh 起得慢是常态）——判定里**不留"不可重试"的假缝**', () => {
  // 原先有个 shouldRetry(render) 恒真 ⇒ 调用点的 `!shouldRetry(...)` 永不成立（死分支）。
  // R17 自查后删掉；这里改为直接断言"三种错误态都产生 error"（重试由调用点无条件执行）。
  for (const r of [
    judged({ address: badAddress('address', 'x') }),
    judged({ address: okAddress('http://127.0.0.1:9'), reachable: false }),
    judged({ address: okAddress('http://127.0.0.1:9'), hasHost: false }),
  ]) {
    assert.equal(r.kind, 'error')
  }
})

test('⭐ R8 非退化 + 全状态不变量：ready⇒非空绝对地址；error⇒非空文案；无第三态', () => {
  const views: PanelView[] = ['', 'monitor']
  const addresses = [
    okAddress('http://127.0.0.1:8123'),        // 真数据
    okAddress('http://127.0.0.1:8123/'),       // 尾斜杠
    badAddress('address', '读不到端口'),        // 地址档
    badAddress('offline', '连不上'),           // 基础路由档
    badAddress('route', '附加路由 500'),       // 附加路由档
  ]
  let ready = 0
  let errors = 0
  for (const hasHost of [true, false]) {
    for (const reachable of [true, false]) {
      for (const view of views) {
        for (const address of addresses) {
          const r = judged({ address, reachable, view, hasHost })
          if (r.kind === 'ready') {
            ready++
            assert.ok(r.src.length > 0, 'ready 的 src 不许为空（那会变成 iframe 永不设 src）')
            assert.match(r.src, /^https?:\/\//, `ready 的 src 必须是绝对地址：${r.src}`)
          } else {
            errors++
            assert.ok(r.message.trim().length > 0, 'error 的文案不许为空')
          }
        }
      }
    }
  }
  // 矩阵必须两个分支都走到，否则它自己退化成恒真（R11：观测量必须能变）
  assert.ok(ready > 0, `矩阵退化：没有 ready 样本（ready=${ready}）`)
  assert.ok(errors > 0, `矩阵退化：没有 error 样本（errors=${errors}）`)

  // 非退化自证：成功态**真的带上了喂进去的地址**（空 vs 空不会过这一条）
  assert.deepEqual(
    judged({ address: okAddress('http://127.0.0.1:8123'), view: 'monitor' }),
    { kind: 'ready', src: 'http://127.0.0.1:8123/monitor' })
  assert.deepEqual(
    judged({ address: okAddress('http://127.0.0.1:8123'), view: '' }),
    { kind: 'ready', src: 'http://127.0.0.1:8123/' })

  // 档位不同 ⇒ 文案不同（否则"档位"是个恒定装饰）
  const a = judged({ address: badAddress('address', 'X') })
  const b = judged({ address: badAddress('offline', 'X') })
  assert.notEqual(a.kind === 'error' && a.message, b.kind === 'error' && b.message)
})

test('viewPath：两个视图各一条路径，且都是绝对路径', () => {
  assert.equal(viewPath(''), '/')
  assert.equal(viewPath('monitor'), '/monitor')
})

// ---------------------------------------------------------------- ④ 不许相对路径 / 未处理态

test('⭐ 缺地址的旧输入 ⇒ 必须 error，**绝不**产出相对 src（迁移前的病）', () => {
  // 迁移前：base = readBootstrap()?.webUrl || '' ⇒ '' ⇒ '' + 'monitor' = 相对 'monitor'
  for (const view of ['', 'monitor'] as PanelView[]) {
    for (const address of [okAddress(''), okAddress('   '), badAddress('address', '注入缺失')]) {
      const r = judged({ address, view })
      assert.equal(r.kind, 'error', `空地址 + 视图 ${JSON.stringify(view)} 必须报错而不是产出 src`)
      assert.notEqual((r as any).src, 'monitor')
      assert.notEqual((r as any).src, '/monitor')
      assert.notEqual((r as any).src, '')
    }
  }
})

test('⭐ 非绝对地址也被拒（相对串一律不许进 iframe）', () => {
  for (const base of ['127.0.0.1:8123', 'monitor', '//example.com', '']) {
    const r = judged({ address: okAddress(base) })
    assert.equal(r.kind, 'error', `非绝对地址 ${JSON.stringify(base)} 必须被拒`)
  }
  const absolute = judged({ address: okAddress('https://example.com/') })
  assert.deepEqual(absolute, { kind: 'ready', src: 'https://example.com/monitor' })
})

test('assertNever：真收到未处理的状态 ⇒ 抛（绝不"什么都不画"）', () => {
  assert.throws(() => assertNever({ kind: 'bogus' } as never), /未处理的 PanelState/)
})
