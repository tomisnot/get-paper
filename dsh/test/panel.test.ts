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
 * 钉三件事：
 *  1. **地址不回落**：端口文件缺失/非法 ⇒ 路由 `503` 且失败体**没有 `base`**，且**不许**
 *     回落到任何默认端口（本项目曾有一份静态默认 `http://127.0.0.1:8080/`）。
 *  2. **取址缝是真的**（资产 `fetchMonitorBase(doFetch, routePath)`）：喂假 fetch 真的决定
 *     结果，且**只**打 `PANEL_CONFIG.ROUTE_PATH`；形状不对（空/非 `http(s)://`）也归
 *     `address` 档——不许让垃圾地址一路走到"连不上"被误读成"权威离线"。
 *  3. **client 入口 import 闭包零 `node:`**（浏览器安全靠结构，不靠树摇的运气）。
 *
 * 〔历史〕原钉 3/4 两件（renderPanel 不空白 / 两跳可诊断）盯的是📄简报 iframe 链，
 * 2026-09-26 第 5 批随该链整体退役（阅读面 = PaperPilot 自家 Web，dsh 只留监控页签）。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, normalize, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { makeMonitorUrlHandler, resolveMonitorBase } from '../src/panel/monitor-url.ts'
// ⚠ `BASIC_ROUTES` / `DEFAULT_ROUTE_PATH` 已随资产 `c44b01f` 迁到**浏览器安全**的 `routes.ts`
//   （原先它们在 node-only 的 `monitor-url.ts` 里 ⇒ client 半取值导入会把 `node:fs` 拖进
//   浏览器 bundle；资产已把这条边界结构化，并加了一条 import 闭包守卫）。
import { BASIC_ROUTES, DEFAULT_ROUTE_PATH } from '../src/panel/routes.ts'
import { PANEL_CONFIG } from '../src/panel/panel-config.ts'
import { fetchMonitorBase, isOriginLike } from '../src/panel/monitor-client.ts'

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
    // 再钉一次"错误体里也不许出现**地址**"，防止有人把默认值塞进文案当"提示"。
    assert.ok(!res.body.includes(HISTORICAL_DEFAULT), `失败体不许含默认地址：${res.body}`)
    // ⚠ 判"没有地址"要用**地址形状**，不许用"有没有数字"：错误文案里本就含出错文件的**路径**，
    // 而临时目录名偶尔带数字（例：`paperpilot-panel-G05d6O`）⇒ 按数字判会**概率性假红**。
    // 我原先写成 `!/\d{2,5}/`，实测在全量 pytest 里偶发红、单跑又绿 —— **那正是 R7 明令
    // 禁止的"守卫靠概率"**，也是我自己制造的一处 flaky（别再把这类红归给环境）。
    assert.ok(!/127\.0\.0\.1|https?:\/\//.test(body.error),
      `错误文案里不该出现地址（不许回落到任何默认地址）：${body.error}`)
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

// ---------------------------------------------------------------- ⑤ 浏览器安全（项目自己的入口）

/**
 * ⭐ **本项目 client 入口的 import 闭包必须零 `node:`**。
 *
 * 为什么项目侧还要一条（资产那条只走**资产目录内部**，入口写死为资产文件）：
 * 真正被 dsh 加载的是**本项目**的 `src/client/index.ts` —— 它 import 资产 + 本项目文件。
 * 而"我先前没撞上 EL 那个 `node:fs` 事故"**纯属树摇的运气**（我恰好没用到带 node 的导出，
 * 整条 import 被摇掉）；资产已把那条边界**结构化**（`routes.ts` 浏览器安全 / `monitor-url.ts`
 * 才碰 `node:fs`），这条判据把**消费侧的运气**也变成**结构**。
 */
test('⭐ 本项目 client 入口的 import 闭包里没有 node:（浏览器安全靠结构，不靠树摇的运气）', () => {
  const here = dirname(fileURLToPath(import.meta.url))
  const src = join(here, '..', 'src')
  const ALLOWED_BARE = new Set(['react', 'react/jsx-runtime'])
  const stripComments = (s: string) => s
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1')
  /** 把 `from './x'` / `from '../x'` 解析成相对 `src/` 的 posix 路径。
   *  ⚠ **别自己剥 `../`**：交给 `join`/`normalize` 处理（第一版剥掉前缀再拼 ⇒ 拼成
   *  `client/panel/...` 这种不存在的路径，判据以 ENOENT 假红——**自己的守卫自己先红过一次**）。 */
  const resolveRel = (fromRel: string, spec: string): string =>
    normalize(join(dirname(fromRel), spec)).split(sep).join('/')

  const queue = ['client/index.ts']
  const seen = new Set<string>()
  const nodeHits: string[] = []
  const bareHits: string[] = []
  while (queue.length) {
    const rel = queue.shift() as string
    if (seen.has(rel)) continue
    seen.add(rel)
    for (const line of stripComments(readFileSync(join(src, rel), 'utf8')).split('\n')) {
      if (/^\s*import\s+type\b/.test(line)) continue      // 类型导入会被剥掉，不是运行时依赖
      const m = line.match(/from\s+'([^']+)'/)
      if (!m) continue
      const spec = m[1] ?? ''
      if (spec.startsWith('node:')) { nodeHits.push(`${rel} → ${spec}`); continue }
      if (spec.startsWith('./') || spec.startsWith('../')) {
        queue.push(resolveRel(rel, spec))
        continue
      }
      if (!ALLOWED_BARE.has(spec)) bareHits.push(`${rel} → ${spec}`)
    }
  }
  assert.deepEqual(nodeHits, [],
    `client 闭包里出现 node 内建：${nodeHits.join('、')} ⇒ 浏览器 bundle 会失败`
    + '（node-only 的实现只许留在 panel/monitor-url.ts，且 client 侧不许 import 它）')
  assert.deepEqual(bareHits, [],
    `client 闭包 import 了非白名单裸包：${bareHits.join('、')} ⇒ 只许宿主注入的 react + 相对路径`)
  // R8 自证：闭包必须**真的走过若干文件**，否则"没命中"只是因为什么都没读到
  assert.ok(seen.size >= 8, `闭包只走了 ${seen.size} 个文件 ⇒ 检查可能没生效：${[...seen]}`)
  // 对偶（不许误报）：闭包**必须包含**资产里那两个纯模块——否则这条可能根本没走进资产
  for (const must of ['panel/panel-data.ts', 'panel/panel-view.ts', 'panel/MonitorTabBody.tsx']) {
    assert.ok(seen.has(must), `闭包里应当有 ${must}：${[...seen]}`)
  }
})


