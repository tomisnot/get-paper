/**
 * paperpilot dsh 插件 —— **HOST 半**（Cordis 插件，函数形态）。
 *
 * ## ⭐ 2026-10-10：它现在只是**载体**（工具面归框架的聚合端点）
 *
 * 用户裁决（**方案 A**）：**工具面只有一条腿** = 框架的聚合端点
 * （dsh 侧由框架的 overlay 插 `@deepseek-ai/dsh-mcp-client`，GP 的工具名形如
 * `mcp__mecha__gp_<tool>`，**读/切写权与开关软件的元工具也在那条腿上**）。
 * ⇒ 本仓原有的**自愈 MCP 桥**（`src/host/gp-hub.ts` + 资产骨架 `mountHostPlugin`）
 * **整块删除**，`src/gp-params.ts`（桥的参数家）一并删除。
 * ⚠ **代价（用户已认）**：**不经 mecha 单独跑 dsh 时，不再出现任何 GP 工具**
 *   —— 那条路本来就不在架构的需求里（AI 面 = 一个聚合端点）。
 *
 * | 事 | 现在谁做 |
 * |---|---|
 * | **AI 工具面**（含读/切写权、开关软件） | ⭐ **框架的聚合端点**（唯一一条腿） |
 * | 监控端点的**地址路由**（读 cockpit 的端口文件现取端口、**不回落**） | ⭐ **框架的 host 插件**（`mecha/dsh-panel/host-plugin/mecha-panel-host.mjs`，按 `MECHA_PANEL_PORT_FILES` 的**绝对**路径现读） |
 * | 面板参数（路由 / 端口文件名 / 标题） | ⭐ **声明**（`Get Paper/mecha_site.py` 的 `Service.monitor`）——本仓不再有第二份 |
 * | 右栏 tab 的挂载 | ⭐ **框架的 client 插件**（`src/client/index.ts` 只留那一行 bootstrap） |
 *
 * ## ⚠ 那它为什么还在（"载体的代价"，如实记）
 *
 * dsh 的加载形状是"**一条 patch entry 带一个包**"：`dsh/cordis.patch.yml` 里
 * `- id: paperpilot / name: './lib/index.mjs'` 既加载**这个包的 host 半**，
 * 也（经 `package.json` 的 `dsh.client`）把**这个包的 client 半**送进浏览器。
 * ⇒ 少了这一件，**右栏一个 tab 都没有，而且那是静默的**。
 *
 * ⚠ 删它需要**同时**改 `mecha_site.py`（`patches` / `plugin_artifacts`）与
 * `package.json` 的 exports —— 而"换一种加载方式"是 **dsh 的机制**，不是本仓能定的
 * ⇒ 保留这个空载体。⚠ 判据：`dsh/test/panel.test.ts`（本文件**必须**是载体）
 *   与框架的 `checks/panel_one_leg_selfcheck.py`（**三家同形**、桥不许回来）。
 *
 * 安全红线：本插件**不** spawn/kill 任何进程、不写任何文件、也不注册任何路由。
 */

/** Cordis 插件显示名（诊断用）。 */
export const name = 'paperpilot'

/** ⚠ **不再需要任何服务**：地址路由归框架、工具面归聚合端点（原先这里要 `tools`）。 */
export const inject: string[] = []

/** 空的挂载点：本插件的价值在"**它被加载**"（client 半随之进 dsh），不在它做了什么。 */
export async function apply(): Promise<void> {
  // 有意为空（见文件头）。⚠ 不注册任何东西 ⇒ 也不留"按了没反应"的死缝。
}
