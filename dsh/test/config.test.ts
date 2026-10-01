/**
 * 端点解析 / 鉴权头 纯函数单测（移植自 re0-mecha-dsh；2026-10-01 改为测**资产版**）：
 *
 * 被测对象不再是 GP 自建的 `src/host/config.ts`（已删），而是
 * ① **资产包** `@mecha/dsh-panel/config.ts`（`resolveHubUrl` / `buildRequestInit`），
 * ② **GP 的参数接线** `src/host/gp-hub.ts`（`gpHubUrlConfig` / `gpResolveHubUrl`）。
 *
 * ⚠ 用例集**一条不少**地保留（R12：换实现不许顺手削弱断言）；只把参数名换成资产的词
 * （`mcpUrl` → `hubUrl`、`mcpToken/mcpHeaders` → `hubToken/hubHeaders`），并在末尾**加了**
 * 两条"GP 的项目值真的接上了"的用例（资产的默认是**空默认**：不传就返回 `''`、且**不读文件**
 * ⇒ 若不显式传，GP 会解析出空串——那正是这两条要钉住的事）。
 */
import assert from 'node:assert/strict'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { buildRequestInit, resolveHubUrl } from '@mecha/dsh-panel/config.ts'
import { gpHubUrlConfig, gpResolveHubUrl } from '../src/host/gp-hub.ts'
import { GP_BRIDGE } from '../src/gp-params.ts'
import { test } from './harness.ts'

test('显式 hubUrl 优先', async () => {
  const url = await gpResolveHubUrl({ hubUrl: 'http://127.0.0.1:9999/mcp' })
  assert.equal(url, 'http://127.0.0.1:9999/mcp')
})

test('端口文件：裸端口 → 拼出 /mcp 端点', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'pp-port-'))
  const file = join(dir, '.mcp-port')
  await writeFile(file, '8765\n', 'utf8')
  const url = await gpResolveHubUrl({ mcpPortFile: file })
  assert.equal(url, 'http://127.0.0.1:8765/mcp')
})

test('端口文件：完整 URL 直接用', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'pp-port-'))
  const file = join(dir, '.mcp-port')
  await writeFile(file, 'http://192.168.1.5:9000/mcp', 'utf8')
  const url = await gpResolveHubUrl({ mcpPortFile: file })
  assert.equal(url, 'http://192.168.1.5:9000/mcp')
})

test('端口文件：含数字的任意串也能提出端口', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'pp-port-'))
  const file = join(dir, '.mcp-port')
  await writeFile(file, 'port=8123 ready', 'utf8')
  const url = await gpResolveHubUrl({ mcpPortFile: file })
  assert.equal(url, 'http://127.0.0.1:8123/mcp')
})

test('buildRequestInit：无 token/头 → undefined', () => {
  assert.equal(buildRequestInit({}), undefined)
})

test('buildRequestInit：token → Authorization: Bearer', () => {
  const init = buildRequestInit({ hubToken: 'secret', hubHeaders: { 'X-Trace': '1' } })
  const headers = (init?.headers ?? {}) as Record<string, string>
  assert.equal(headers['Authorization'], 'Bearer secret')
  assert.equal(headers['X-Trace'], '1')
})

test('⭐ GP 的项目值真的接上了（R17）：默认端点/端口文件名都来自 gp-params，且读取回落不抛', async () => {
  const cfg = gpHubUrlConfig({})
  assert.equal(cfg.mcpPortFile, '.mcp-port')
  assert.equal(cfg.mcpPortFile, GP_BRIDGE.mcpPortFile)
  assert.equal(cfg.defaultUrl, GP_BRIDGE.defaultUrl)
  // 反面：端口文件名可被调用方覆盖（不是写死的常量）
  assert.equal(gpHubUrlConfig({ mcpPortFile: '  /tmp/x.port  ' }).mcpPortFile, '/tmp/x.port')
  // 端口文件读不到 ⇒ **回落** GP 的默认端点（不抛，交给桥后台重连）
  //（原「没有端口文件 → 回落」独立用例与本节断言同一事实 ⇒ 合并，R13/D1）
  assert.equal(await gpResolveHubUrl({ mcpPortFile: '/nonexistent/.mcp-port' }),
    GP_BRIDGE.defaultUrl)
  assert.equal(GP_BRIDGE.defaultUrl, 'http://127.0.0.1:8780/mcp')
})

test('⭐ 资产默认是"空默认、不猜端口"（不传项目值时解析出空串，不是某个端口）', async () => {
  assert.equal(await resolveHubUrl({}), '')
  assert.equal(await resolveHubUrl({ mcpPortFile: '/nonexistent/.mcp-port' }), '')
})
