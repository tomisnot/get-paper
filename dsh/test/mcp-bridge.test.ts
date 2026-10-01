/**
 * 桥的**项目侧**判据 —— 只守一件资产守不了的事。
 *
 * ## 为什么这里只剩一条用例（R13：删机制要把它守的承诺改写成行为级判据）
 *
 * 桥的**状态机**（首连成功 / 404 自愈 / ECONNREFUSED 自愈 / 首连失败不抛 / 重连耗尽抛可读离线 /
 * 首连失败后再调用也抛可读离线 / 非连接类错误不被冒充成"服务离线" / 业务错误原样抛 /
 * 鸭子类型判别 / offlineHint 与 logLabel 是真参数 …）现在由**资产自带的共享单测**逐例覆盖：
 * `../src/panel/mcp-bridge.test.ts`（13 例，随资产发货，并在本仓 `npm test` 与
 * `tests/test_dsh_panel.py` 里**真跑**）⇒ 本文件再写一遍同义断言就是**两个守卫守同一事实**
 * （本工程明令禁止）。
 *
 * 本文件留下的这条是**资产结构上守不了**的：`gp-params.ts` 里的项目值
 * （`logLabel` / `offlineHint`）**必须真的走到桥上**——资产不知道 GP 填了什么，
 * 而"声明了却没人读"正是本工程反复治的死缝（R17）。
 * ⚠ 它同时是 `gpBridgeOptions` 的消费者证据：删掉它，`gp-params.ts` 就没人读了。
 */
import assert from 'node:assert/strict'
import { MechaMcpBridge } from '../src/panel/mcp-bridge.ts'
import { gpBridgeOptions } from '../src/host/gp-hub.ts'
import { GP_BRIDGE } from '../src/gp-params.ts'
import { test } from './harness.ts'

const noSleep = (): Promise<void> => Promise.resolve()
const notFound = (): Error & { code?: number } =>
  Object.assign(new Error('Session not found'), { code: 404 })

/** 最小假会话：`connectFails` 时连 `listTools` 都抛（模拟"服务没起来"）。 */
class FakeSession {
  onClose?: () => void
  closed = false
  connectFails = false
  readonly sessionId: string
  private readonly behavior: () => Promise<unknown>

  constructor(sessionId: string, behavior: () => Promise<unknown>) {
    this.sessionId = sessionId
    this.behavior = behavior
  }

  async listTools(): Promise<{ tools: { name: string; description?: string; inputSchema?: unknown }[] }> {
    if (this.connectFails) throw notFound()
    return { tools: [{ name: 'read_digest', description: 'd', inputSchema: {} }] }
  }

  async callTool(_name: string, _args: unknown): Promise<unknown> {
    return this.behavior()
  }

  async close(): Promise<void> {
    this.closed = true
  }
}

test('⭐ GP 的参数真的进了桥（R17）：logLabel / offlineHint 经 gpBridgeOptions 生效', async () => {
  // 形状：首连成功 → 一次调用失败（触发自愈）→ **重连也失败** ⇒ 才落到"服务离线…"那一支
  //（"首连就失败"那一支现在也抛可读离线错误，但那由资产的共享单测钉，不在此重复）。
  const stale = new FakeSession('stale', async () => { throw notFound() })
  const dead = new FakeSession('dead', async () => { throw notFound() })
  dead.connectFails = true
  let n = 0
  const bridge = new MechaMcpBridge(gpBridgeOptions(async () => {
    n++
    return (n === 1 ? stale : dead) as never
  }, {
    sleep: noSleep,
    reconnect: { initialDelayMs: 0, maxDelayMs: 0, maxAttempts: 1 },
  }))

  await bridge.start()
  assert.equal(bridge.getStatus(), 'ready')     // 自证前置：真的走到"调用失败 → 重连"那一支

  await assert.rejects(
    () => bridge.callTool('read_digest', {}),
    (err: Error) => {
      assert.ok(err.message.startsWith(`[${GP_BRIDGE.logLabel}]`),
        `日志前缀不是 GP 的 logLabel：${err.message}`)
      assert.ok(err.message.includes(GP_BRIDGE.offlineHint),
        `GP 的 offlineHint 没进离线文案：${err.message}`)
      // 反面：不许退回资产的中性默认句（参数要**替换**它，不是叠加/被忽略）
      assert.ok(!err.message.includes('请确认软件已启动'),
        `中性默认句仍在 ⇒ GP 的值没被采用：${err.message}`)
      return true
    },
  )
})
