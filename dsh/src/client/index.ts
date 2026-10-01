/**
 * paperpilot dsh 插件 —— **CLIENT 入口**（浏览器侧 Cordis 插件）。
 *
 * ⚠ **2026-09-26 第 5 批（用户裁决）**：「📄 简报」iframe 那条链**整体退役**——
 * 阅读面就是 PaperPilot 自己的 Web，不必在 dsh 里再套一层；dsh 侧栏只保留
 * **「◈ 监控」原生页签**（AI 干了什么、走的哪道门）。没有第二个面板了，
 * 原先"两面板共用右栏必须互斥"的约束随之退役（不是缺陷被修，是需要性消失）。
 *
 * 贡献：① 会话头部「◈ 监控」按钮（`conversation.session.header.actions`）；
 * ② 右栏页签类型 + body——数据/呈现全部来自共享资产 `dsh-panel/`
 * （`panel-data.ts` / `panel-view.ts` / `MonitorTabBody.tsx`），**本项目只提供
 * 参数块**（`panel-config.ts`），注册形状抄 EL。注册与副作用经 `ctx.effect` 可逆。
 *
 * 地址唯一来源仍是：host 同源只读路由 `PANEL_CONFIG.ROUTE_PATH`（读项目根
 * `PANEL_CONFIG.PORT_FILE` 的裸端口，**绝不回落默认端口**）——由共享资产
 * `monitor-client.ts` 自己消费，本入口不再碰。
 *
 * ⚠ client 半经 tsdown 打成 CJS + `__ModuleLoader__.load` 包装；改本目录源码后须
 * `npm run bundle` 并重启 dsh 才生效。
 */
import type { Context } from '@deepseek-ai/cordis'
import { configurePanel, panelConfig } from '@mecha/dsh-panel/panel-config.ts'
import { MonitorTabBody } from '@mecha/dsh-panel/MonitorTabBody.tsx'
import { GP_PANEL } from '../gp-params.ts'
import { MonitorButton, type MonitorInjected } from './MonitorButton.tsx'
import { ReviewSopButton } from './ReviewSopButton.tsx'

/** Cordis 插件名（与 host 半一致）。 */
export const name = 'paperpilot'

/** 需要 web client 的服务：slots（挂按钮）+ layout（开右栏）
 *  + `sidebarRight` / `sidebarRightTabs`（原生页签）。
 *  Cordis ctx 是代理：未在 inject 声明的服务一访问就抛，故用到的必须在此声明。 */
export const inject = ['slots', 'layout', 'sidebarRight', 'sidebarRightTabs']

/** 监控面板的右栏页签 id；`kind` 与 `sidebar.right.pane.tab` 的 key **必须一致**。 */
const MONITOR_TAB_ID = 'pp-monitor'

export async function apply(ctx: Context): Promise<void> {
  // ⚠ client 半是**另一份 bundle/另一个进程** ⇒ 面板参数要在这里**再注入一次**
  // （资产不存项目值；未注入 ⇒ `panelConfig()` 抛，面板宁可炸也不假装空）。
  const releasePanel = configurePanel({ ...GP_PANEL })
  await ctx.effect(() => {
    // 「◈ 监控」按钮：开右栏 → 打开我们的页签（按钮不自己开合，宿主侧动作，与 EL 同形）。
    const disposeMonitorBtn = ctx.slots.inject('conversation.session.header.actions', () =>
      ctx.slots.register(
        { name: 'conversation.session.header.actions', id: MONITOR_TAB_ID, order: 201,
          label: '◈ 监控',
          inject: (): MonitorInjected => ({
            openCockpit: () => {
              ctx.layout?.openRightbar?.(true, false)
              try { ctx.sidebarRight?.openTab?.(MONITOR_TAB_ID) } catch { /* 降级：右栏开了但没切页签 */ }
            },
          }) } as never,
        MonitorButton,
      ))

    // 「☀ 评审今日」（D1）：把固定评审 SOP 一键复制进剪贴板（零宿主 API 假设）。
    const disposeSopBtn = ctx.slots.inject('conversation.session.header.actions', () =>
      ctx.slots.register(
        { name: 'conversation.session.header.actions', id: 'pp-review-sop', order: 202 },
        ReviewSopButton,
      ))

    // 页签类型 + body：零 react 判据在资产自测里；.tsx 壳只过 typecheck。
    const releaseTabType = ctx.sidebarRightTabs?.register?.({
      id: MONITOR_TAB_ID, kind: MONITOR_TAB_ID, title: () => panelConfig().title,
    }) ?? null
    const disposeTabBody = ctx.slots.inject('sidebar.right.pane.tab', () =>
      ctx.slots.register(
        { name: 'sidebar.right.pane.tab', key: MONITOR_TAB_ID } as never,
        MonitorTabBody as never,
      ))

    return () => {
      try { disposeSopBtn?.() } catch { /* ignore */ }
      if (disposeTabBody) { try { disposeTabBody() } catch { /* ignore */ } }
      if (typeof releaseTabType === 'function') { try { releaseTabType() } catch { /* ignore */ } }
      try { disposeMonitorBtn?.() } catch { /* ignore */ }
      releasePanel()       // 注入态清回"未注入"（可逆）
    }
  }, 'paperpilot: monitor tab')
}
