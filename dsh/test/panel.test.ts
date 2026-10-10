/**
 * PaperPilot **面板判据**（项目自己那份）。
 *
 * 跑：`npm run test:panel`（= `node --test test/panel.test.ts`，**裸跑**：不加任何旗标/loader）
 *
 * 面板来自**依赖包** `@mecha/dsh-panel`（`file:` junction，唯一副本在 mecha 仓）。
 *
 * ## 2026-10-09：监控面（参数 + 地址路由）**上提进框架** ⇒ 本文件大幅收窄
 *
 * | 原来钉的 | 现在由谁守 |
 * |---|---|
 * | "GP 注入的面板参数是项目值 + 注入往返" | ⭐ **结构性消灭**：本仓**不再有**参数（`gp-params.ts` 里那个常量已删） |
 * | "声明与参数家同值"（两处对拍） | ⭐ mecha 的 `phase13_panel_decl_selfcheck`（**夹具 ↔ 三家真声明对拍**，读的是真声明） |
 * | ① 地址不回落 / 坏内容 503 / 失败体无 `base` | ⭐ **框架的 host 插件**：`mecha/dsh-panel/host-plugin/mecha-panel-host.test.ts`（含与资产实现的**等价判据**）；资产那份单测仍覆盖 `503` 语义 |
 * | ② client 取址打的是**本项目**的路由 + 落 `address` 档 + `isOriginLike` | ⭐ 本仓**已经没有取址代码**了（那条路由与取址都归框架）⇒ 这些是**资产**的形态，资产自测覆盖 |
 *
 * ## 本文件现在钉**四**件（都是"本仓自己的"）
 *
 *  1. ⭐ **那一行 bootstrap**：client 半必须接上**框架的** client 插件
 *     （少了它 = 右栏一个 tab 都没有，而那不会报错）；⚠ **本仓的自定义 UI 已删**
 *     （用户裁决：「☀ 评审今日」不起作用、还挤在框架那个唯一的「◈ 监控」按钮旁边）；
 *  2. ⭐ **不许再自己注册面板/地址路由**（`GP_PANEL` / `panelConfig` / 手写 tab 注册 **不许回来**）：
 *     ⚠ 同一条 `routePath` 注册两次是接线错误（谁生效取决于注册顺序）；
 *  3. **反面语料**：本仓历史的静态默认地址 `http://127.0.0.1:8080` 不许出现在 dsh 源码里
 *     （它当年是"回落假绿"的现场；现在连路由都不在本仓了，这条改成**源码级**的反面语料）；
 *  4. client 入口 import 闭包零 `node:`（浏览器安全靠结构，不靠树摇的运气）。
 *  5. ⭐ **2026-10-10（方案 A）：host 半必须是空载体** —— 那条"项目自己接 MCP"的
 *     自愈桥整块删除了；`src/host/` 不存在、`gp-params.ts` 不存在、`mountHostPlugin`
 *     与 `@mecha/dsh-panel/host/` **不许**出现在本仓源码里（工具面只有一条腿）。
 *     ⚠ 同批删掉的还有 `test/mcp-bridge.test.ts` / `test/config.test.ts` /
 *     `test/run.ts` / `test/harness.ts`（它们测的就是那座桥）。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, normalize, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = join(HERE, '..', 'src')
const REPO = join(HERE, '..', '..')

/** 本项目**历史**的静态默认地址（迁移前写在插件配置里）——只作为**反面语料**。 */
const HISTORICAL_DEFAULT = 'http://127.0.0.1:8080'

const read = (rel: string): string => readFileSync(join(SRC, rel), 'utf8')

/** 去掉注释：本文件几处"不许出现 X"的断言看的是**代码**，而注释里解释
 *  "X 已删/不许回来"是**该留的**（否则下一个人查不到为什么不能走老路）。 */
const stripComments = (s: string): string => s
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/(^|[^:])\/\/.*$/gm, '$1')

/** 本仓 dsh 的源码文件（**含 `.tsx`**：死文件曾经就是这么漏掉的）。 */
function sourceFiles(): string[] {
  const out: string[] = []
  const walk = (dir: string): void => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, e.name)
      if (e.isDirectory()) walk(p)
      else if (/\.tsx?$/.test(e.name)) out.push(relative(SRC, p).replace(/\\/g, '/'))
    }
  }
  walk(SRC)
  return out
}

function codeHits(needle: string): string[] {
  return sourceFiles().filter((f) => stripComments(read(f)).includes(needle))
}

// ---------------------------------------------------------------- 2 不许再自己注册
test('⭐ 监控面归框架 ⇒ 本仓不许再有 GP_PANEL / panelConfig / 自己的 tab 注册', () => {
  // ⚠ **2026-10-09 改**：本仓 client 半退成**一行 bootstrap**（自定义 UI 已按用户裁决删除）
  //   ⇒ 判据从"必须调 `applyMechaPanels(ctx)`"改成"必须是那一行再导出"（与 EL 同形）。
  assert.match(read('client/index.ts'),
    /export \{ apply, name, inject \} from '@mecha\/dsh-panel\/panel\/auto\.ts'/,
    'client 半必须是那一行 bootstrap（少了它 = 右栏一个 tab 都没有，且不报错）')
  for (const needle of ['GP_PANEL', 'panelConfig', 'makeMonitorUrlHandler', 'configurePanel']) {
    const hits = codeHits(needle)
    assert.deepEqual(hits, [], `${needle} 又回来了（路由/参数的第二处真值）：${hits.join('、')}`)
  }
  // ⚠ 只禁"**自己注册监控 tab 类型**"；`inject` 里声明 `sidebarRightTabs` 是**必须的**
  //   （cordis 的 ctx 是代理：框架那份插件要经 GP 的 ctx 用这些服务，不声明就抛）
  //   —— 把"声明服务"当"自己注册"来禁会造出一条**假判据**（实测踩到）。
  assert.doesNotMatch(stripComments(read('client/index.ts')),
    /sidebarRightTabs\s*\.\s*register/,
    '本仓不许再自己注册右栏 tab 类型（监控 tab 归框架）')
  // ⚠ host 半**不许**再传 panelConfig（那是"同一条 routePath 注册两次"的来源）
  assert.doesNotMatch(stripComments(read('index.ts')), /panelConfig\s*:/,
    'host 半不许再传 panelConfig（框架的 host 插件按声明注册那条地址路由）')
})

// ---------------------------------------------------------------- 3 反面语料
test('反面语料：历史静态默认地址 8080 不在本仓 dsh 源码里', () => {
  const hits = sourceFiles().filter((f) => read(f).includes(HISTORICAL_DEFAULT))
  assert.deepEqual(hits, [],
    `dsh 源码里出现了历史默认地址（当年"回落假绿"的现场）：${hits.join('、')}`)
})

// ---------------------------------------------------------------- 5 浏览器安全（项目自己的入口）
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
  // ⚠ 2026-10-01（依赖化改造）：面板改从**包** `@mecha/dsh-panel` 取（junction）⇒ 闭包走到
  //   **包边界就不再往下走**（本判据不解析 node_modules）。分工因此变成：
  //   · 包**内部**那条"零 `node:`"由**资产自己的**闭包守卫守（`panel/monitor-client.test.ts`，
  //     随包发货，由框架门禁 `mecha/checks/dsh_panel_selfcheck.py` 跑）；
  //   · **本判据守本仓这一侧**：GP 自己的文件不许带 `node:`，且裸包只许宿主注入的 `react`
  //     与这个资产包（别的裸包一律拒）。
  const ALLOWED_BARE = new Set(['react', 'react/jsx-runtime'])
  const ASSET_PKG = '@mecha/dsh-panel'
  /** 把 `from './x'` / `from '../x'` 解析成相对 `src/` 的 posix 路径。
   *  ⚠ **别自己剥 `../`**：交给 `join`/`normalize` 处理（第一版剥掉前缀再拼 ⇒ 拼成
   *  `client/panel/...` 这种不存在的路径，判据以 ENOENT 假红——**自己的守卫自己先红过一次**）。 */
  const resolveRel = (fromRel: string, spec: string): string =>
    normalize(join(dirname(fromRel), spec)).split(sep).join('/')

  const queue = ['client/index.ts']
  const seen = new Set<string>()
  const nodeHits: string[] = []
  const bareHits: string[] = []
  const assetHits: string[] = []
  while (queue.length) {
    const rel = queue.shift() as string
    if (seen.has(rel)) continue
    seen.add(rel)
    for (const line of stripComments(readFileSync(join(SRC, rel), 'utf8')).split('\n')) {
      if (/^\s*import\s+type\b/.test(line)) continue      // 类型导入会被剥掉，不是运行时依赖
      const m = line.match(/from\s+'([^']+)'/)
      if (!m) continue
      const spec = m[1] ?? ''
      if (spec.startsWith('node:')) { nodeHits.push(`${rel} → ${spec}`); continue }
      if (spec.startsWith('./') || spec.startsWith('../')) {
        queue.push(resolveRel(rel, spec))
        continue
      }
      if (spec === ASSET_PKG || spec.startsWith(`${ASSET_PKG}/`)) {
        assetHits.push(`${rel} → ${spec}`)                // 包边界：不再深入（见上）
        continue
      }
      if (!ALLOWED_BARE.has(spec)) bareHits.push(`${rel} → ${spec}`)
    }
  }
  assert.deepEqual(nodeHits, [],
    `client 闭包里出现 node 内建：${nodeHits.join('、')} ⇒ 浏览器 bundle 会失败`
    + '（node-only 的实现只许留在资产包的 monitor-url.ts 一侧，且 client 侧不许 import 它）')
  assert.deepEqual(bareHits, [],
    `client 闭包 import 了非白名单裸包：${bareHits.join('、')}`
    + ' ⇒ 只许宿主注入的 react + 资产包 @mecha/dsh-panel + 相对路径')
  // R8 自证：闭包必须**真的走过若干文件**，否则"没命中"只是因为什么都没读到
  // ⚠ 走下界**跟着事实走**：`seen` 只记**本仓自己的**文件（走到 `@mecha/dsh-panel`
  //   就停在包边界、不再深入，见上面的 `assetHits`）。
  // ⚠ **2026-10-09 改**：本仓 client 半现在**只剩那一行 bootstrap**（自定义 UI 已删，
  //   见下一条）⇒ `seen` 就只有入口自己；下界随之从 2 降到 1（不是放宽，是事实变了）。
  assert.ok(seen.size >= 1, `闭包一个文件都没走过 ⇒ 检查没生效：${[...seen]}`)
  assert.ok(seen.has('client/index.ts'), `闭包该从本仓入口开始：${[...seen]}`)
  // ⭐ **反向断言**：GP 自己那个「☀ 评审今日」按钮**已删**（用户裁决：它不起作用 + 挤在
  //   框架那个唯一的「◈ 监控」按钮旁边）⇒ 源码里不许再有它（R13：删了要留"不许回来"）。
  for (const needle of ['ReviewSopButton', 'pp-review-sop', '评审今日']) {
    const hits = codeHits(needle)
    assert.deepEqual(hits, [], `${needle} 又回来了（那个按钮已按用户裁决删除）：${hits.join('、')}`)
  }
  //   ⭐ 走到了**资产包**这件事由 `assetHits` 记录（`seen` 按设计**不含**包内文件），
  //   所以"必须接上框架那份插件"那条要查 `assetHits`，不能查 `seen`。
  assert.ok(assetHits.some((h) => h.includes('panel/auto.ts')),
    `client 半应当接上框架那份客户端插件（panel/auto.ts）：${assetHits.join('、')}`)
  assert.ok(assetHits.length >= 1,
    `client 半没有从 ${ASSET_PKG} 取面板（是不是还在 import 旧副本？）：${[...seen]}`)
})

// ---------------------------------------------------------------- 唯一家还在
test('⭐ 那个唯一家还在：`mecha_site.py` 里有 monitor=MonitorSpec(...)', () => {
  // ⚠ 本判据**只**断言"声明还在"；**值**由 mecha 的 `phase13_panel_decl_selfcheck`
  //   拿**真声明**对拍（夹具↔真声明），不在这里抄一遍（抄一遍就是第二处真值）。
  const decl = readFileSync(join(REPO, 'mecha_site.py'), 'utf8')
  assert.match(decl, /monitor=MonitorSpec\(/, '声明里没有 monitor=MonitorSpec(...)')
  assert.match(decl, /route_path="/, '声明里 monitor 缺 route_path（框架按它注册地址路由）')
  assert.match(decl, /port_file="/, '声明里 monitor 缺 port_file（框架按它现取端口）')
})

// ---------------------------------------------------------------- ⭐ 空载体（方案 A）
test('⭐ host 半是**空载体**：桥那套不许回来（工具面只有一条腿 = 框架的聚合端点）', () => {
  // ⚠ 这是 **2026-10-10 用户裁决（方案 A）** 的机器守卫。为什么非要钉：
  //   桥是"每个项目自己接 MCP"时代的产物，而聚合端点是**后加的** ⇒ 两条腿并存时
  //   ① 只开一家也会加载别家的桥（`merge_dsh` 的 patches 是全站点并集）；② 工具名重复；
  //   ③ ⭐ **能力不等**：写权/开关元工具只在聚合端点那条腿上（GP 侧实测：只开 GP 时
  //      别家的桥照样被加载、而框架元工具只在聚合面上）。
  //   ⇒ 删掉桥之后，三家**接法相同**；⚠ 代价（用户已认）：不经 mecha 单独跑 dsh 时无工具。
  const idx = stripComments(read('index.ts'))
  for (const needle of ['mountHostPlugin', 'hostPluginInject', 'dsh-panel/host']) {
    assert.ok(!idx.includes(needle), `host 半不许再出现 ${needle}（桥已按方案 A 删除）`)
  }
  assert.match(idx, /export const inject: string\[\] = \[\]/,
    'host 半必须**不依赖任何服务**（`inject = []`）——它只是载体')
  assert.match(idx, /export async function apply\(\)/,
    'host 半的 apply **不许**再接受 ctx/config（没有桥要装了）')

  // 结构性：桥的家（`src/host/`）、桥的参数家（`gp-params.ts`）、以及测桥的那套测试
  // 都**不该存在**（R13：删机制要留"不许回来"）。
  const src = sourceFiles()
  assert.ok(!src.some((f) => f.startsWith('host/')),
    `src/host/ 又出现了（桥的家）：${src.join('、')}`)
  assert.ok(!src.includes('gp-params.ts'),
    `gp-params.ts 又出现了（桥的参数家）：${src.join('、')}`)
  const tests = readdirSync(join(HERE)).filter((f) => f.endsWith('.ts'))
  for (const gone of ['mcp-bridge.test.ts', 'config.test.ts', 'run.ts', 'harness.ts']) {
    assert.ok(!tests.includes(gone), `${gone} 又回来了（它测的就是已删的桥）`)
  }
  // R8 自证：本判据确实读到了源码（不是"文件都不存在所以全绿"）
  assert.ok(src.includes('index.ts') && src.includes('client/index.ts'),
    `判据没读到本仓源码：${src.join('、')}`)
})
