/** 测试入口：`node test/run.ts`（或 npm test）。**裸跑**：不需要任何旗标或 loader，见 README 那两条实测。 */
import './mcp-bridge.test.ts'
import './config.test.ts'
import { runAll } from './harness.ts'

await runAll('paperpilot-dsh')
