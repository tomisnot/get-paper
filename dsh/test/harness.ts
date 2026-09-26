/**
 * 极简进程内测试 runner（零依赖、不 spawn 子进程）。
 *
 * 为什么不用 `node --test`：它的按文件 spawn 在某些受限环境（沙箱/管道受限）会 EPERM；
 * 且本插件的测试全是纯逻辑 + node:assert，进程内顺序执行足够。API 与 node:test 兼容到
 * 够用（test(name, fn) + 进程退出码）。
 */
type Fn = () => void | Promise<void>

const tests: Array<{ name: string; fn: Fn }> = []

export function test(name: string, fn: Fn): void {
  tests.push({ name, fn })
}

export async function runAll(label: string): Promise<void> {
  let failed = 0
  for (const { name, fn } of tests) {
    try {
      await fn()
      console.log(`  ✓ ${name}`)
    } catch (err) {
      failed++
      console.error(`  ✖ ${name}`)
      console.error(`    ${err instanceof Error ? err.message : String(err)}`)
    }
  }
  console.log(`${label}: ${tests.length - failed}/${tests.length} passed`)
  if (failed > 0) process.exitCode = 1
}
