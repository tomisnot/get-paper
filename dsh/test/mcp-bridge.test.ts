/**
 * 桥的**项目侧**判据 —— 只守一件资产守不了的事：**GP 的值真的进到用户可见的面**。
 *
 * ## 为什么这里只剩一条用例（R13：删机制要把它守的承诺改写成行为级判据）
 *
 * 桥的状态机、骨架的挂载/收尾/端点现解、`logLabel`/`offlineHint` 作为**真参数**的 R17 证明，
 * 现在分别由资产自带的 `mcp-bridge.test.ts` 与 `mount-host-plugin.test.ts` 覆盖
 * （随包发货，由**框架门禁** `mecha/checks/dsh_panel_selfcheck.py` 跑）⇒ 本文件不再重抄。
 *
 * 留下这条的理由：**"GP 填的那几个值恰好是这些、且它们会出现在用户/AI 看得见的地方"**
 * 是 GP 的内容（资产不知道 GP 填了什么），也只有 GP 能验。它同时是 `gp-params.ts`
 * 那几个值的**消费者证据**（删掉它，那几行就没人读了）。
 */
import assert from 'node:assert/strict'
import { mountHostPlugin, type HostContext } from '@mecha/dsh-panel/mount-host-plugin.ts'
import { GP_BRIDGE, GP_PANEL, GP_TOOLS } from '../src/gp-params.ts'
import { test } from './harness.ts'

const notFound = (): Error & { code?: number } =>
  Object.assign(new Error('Session not found'), { code: 404 })

/** 最小假会话：`connectFails` 时连 `listTools` 都抛（模拟"服务没起来"）。 */
class FakeSession {
  onClose?: () => void
  closed = false
  connectFails = false
  private readonly behavior: () => Promise<unknown>
  constructor(behavior: () => Promise<unknown>) { this.behavior = behavior }
  async listTools() {
    if (this.connectFails) throw notFound()
    return { tools: [{ name: 'read_digest', description: 'd', inputSchema: {} }] }
  }
  async callTool(_name: string, _args: unknown) { return this.behavior() }
  async close() { this.closed = true }
}

test('⭐ GP 的值真的进了用户可见的面（R17）：logLabel / offlineHint / serverName 都生效', async () => {
  // 形状：首连成功 → 一次调用失败（触发自愈）→ **重连也失败** ⇒ 落到"服务离线…+offlineHint"那一支。
  const stale = new FakeSession(async () => { throw notFound() })
  const dead = new FakeSession(async () => { throw notFound() })
  dead.connectFails = true
  let n = 0

  const registered: Array<{ name: string; execute: (a: unknown, e: unknown) => Promise<unknown> }> = []
  const ctx: HostContext = {
    logger: { info: () => {}, warn: () => {}, error: () => {} },
    tools: { register: def => { registered.push(def as never); return () => {} } },
    effect: async fn => { await fn() },     // 同步跑一遍挂载即可（本用例不验收尾）
    inject: () => {},                       // 不假装有 webServer：路由那条跳过
  }

  await mountHostPlugin(ctx, {
    serverName: GP_TOOLS.serverName,
    logLabel: GP_BRIDGE.logLabel,
    projectRoot: '.',
    resolveUrl: async () => 'http://127.0.0.1:1/mcp',   // 有假会话 ⇒ 骨架不会调它
    clientInfo: { ...GP_BRIDGE.clientInfo },
    offlineHint: GP_BRIDGE.offlineHint,
    reconnect: { initialDelayMs: 0, maxDelayMs: 0, maxAttempts: 1 },
    panelConfig: { ...GP_PANEL },
    sessionFactory: async () => (n++ === 0 ? stale : dead) as never,
  })

  // ① 工具进了 GP 的命名空间（`serverName` 生效）
  assert.equal(registered.length, 1)
  assert.equal(registered[0]!.name, `mcp__${GP_TOOLS.serverName}__read_digest`)

  // ② 离线错误带 GP 的 `logLabel` 前缀与 `offlineHint`（两者都是 GP 的值）
  await assert.rejects(
    () => registered[0]!.execute({}, {}),
    (err: Error) => {
      assert.ok(err.message.startsWith(`[${GP_BRIDGE.logLabel}]`),
        `日志前缀不是 GP 的 logLabel：${err.message}`)
      assert.ok(err.message.includes(GP_BRIDGE.offlineHint),
        `GP 的 offlineHint 没进离线文案：${err.message}`)
      // 反面：不许退回资产的中性默认句（值要**替换**它，不是被忽略）
      assert.ok(!err.message.includes('请确认软件已启动'),
        `中性默认句仍在 ⇒ GP 的值没被采用：${err.message}`)
      return true
    },
  )
})
