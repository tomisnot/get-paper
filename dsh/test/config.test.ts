/**
 * 端点解析 / 鉴权头 纯函数单测（移植自 re0-mecha-dsh）。
 */
import assert from 'node:assert/strict'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { buildRequestInit, resolveMcpUrl } from '../src/host/config.ts'
import { test } from './harness.ts'

test('显式 mcpUrl 优先', async () => {
  const url = await resolveMcpUrl({ mcpUrl: 'http://127.0.0.1:9999/mcp' })
  assert.equal(url, 'http://127.0.0.1:9999/mcp')
})

test('端口文件：裸端口 → 拼出 /mcp 端点', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'pp-port-'))
  const file = join(dir, '.mcp-port')
  await writeFile(file, '8765\n', 'utf8')
  const url = await resolveMcpUrl({ mcpPortFile: file })
  assert.equal(url, 'http://127.0.0.1:8765/mcp')
})

test('端口文件：完整 URL 直接用', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'pp-port-'))
  const file = join(dir, '.mcp-port')
  await writeFile(file, 'http://192.168.1.5:9000/mcp', 'utf8')
  const url = await resolveMcpUrl({ mcpPortFile: file })
  assert.equal(url, 'http://192.168.1.5:9000/mcp')
})

test('端口文件：含数字的任意串也能提出端口', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'pp-port-'))
  const file = join(dir, '.mcp-port')
  await writeFile(file, 'port=8123 ready', 'utf8')
  const url = await resolveMcpUrl({ mcpPortFile: file })
  assert.equal(url, 'http://127.0.0.1:8123/mcp')
})

test('没有端口文件 → 回落默认（不抛，交给桥重连）', async () => {
  const url = await resolveMcpUrl({ mcpPortFile: '/nonexistent/.mcp-port' })
  assert.equal(url, 'http://127.0.0.1:8780/mcp')
})

test('buildRequestInit：无 token/头 → undefined', () => {
  assert.equal(buildRequestInit({}), undefined)
})

test('buildRequestInit：token → Authorization: Bearer', () => {
  const init = buildRequestInit({ mcpToken: 'secret', mcpHeaders: { 'X-Trace': '1' } })
  const headers = (init?.headers ?? {}) as Record<string, string>
  assert.equal(headers['Authorization'], 'Bearer secret')
  assert.equal(headers['X-Trace'], '1')
})
