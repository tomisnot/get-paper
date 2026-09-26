/** 测试入口：`node --import tsx test/run.ts`（或 npm test）。 */
import './mcp-bridge.test.ts'
import './config.test.ts'
import { runAll } from './harness.ts'

await runAll('paperpilot-dsh')
