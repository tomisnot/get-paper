/**
 * paperpilot dsh 插件 —— **CLIENT 入口**（浏览器侧 Cordis 插件）。
 *
 * ⚠ **2026-09-26 第 5 批（用户裁决）**：「📄 简报」iframe 那条链**整体退役**——
 * 阅读面就是 PaperPilot 自己的 Web，不必在 dsh 里再套一层；dsh 侧栏只保留
 * **「◈ 监控」原生页签**（AI 干了什么、走的哪道门）。
 *
 * ⭐ **2026-10 阶段①**：**「◈ 监控」按钮与右栏页签注册都归框架**了
 * （`mountClientPanel`：按钮全生态**唯一一个**、语义 = **开右栏**；页签按 §7.3.3 命名）。
 * 本仓原来的两段手写注册（`conversation.session.header.actions` 的监控按钮、
 * `sidebarRightTabs` + `sidebar.right.pane.tab`）随之删除 —— 三家里各写一遍时，
 * `title: () => panelConfig().title` 是**渲染时求值**的 thunk ⇒ 三家装进同一个 dsh 时
 * 三个页签的标题与取数路由**全落到"最后注册那一份"**（且不报错）。
 *
 * ⭐ **本仓仍贡献自己的东西**（"自定义 UI"那条路**保留**）：
 * 「☀ 评审今日」按钮（`ReviewSopButton`，一键复制固定评审 SOP）**留在本文件**——
 * 框架管的是**监控面板那一份**，不是"项目不许有别的按钮"。
 *
 * 数据/呈现全部来自**依赖包** `@mecha/dsh-panel`（`panel-data.ts` / `panel-view.ts` /
 * `MonitorTabBody.tsx`），本项目只**交出参数**（`GP_PANEL`，值在 `gp-params.ts`）。
 * 注册与副作用经 `ctx.effect` 可逆。
 *
 * 地址唯一来源仍是：host 同源只读路由（注入的 `routePath`；读项目根注入的 `portFile` 的裸端口，
 * **绝不回落默认端口**）——由包里的 `monitor-client.ts` 自己消费，本入口不再碰。
 *
 * ⚠ client 半经 tsdown 打成 CJS + `__ModuleLoader__.load` 包装；改本目录源码后须
 * `npm run bundle` 并重启 dsh 才生效。
 */
import type { Context } from '@deepseek-ai/cordis'
import { mountClientPanel } from '@mecha/dsh-panel/panel/mount-client-plugin.ts'
import { MonitorTabBody } from '@mecha/dsh-panel/panel/MonitorTabBody.tsx'
import { GP_PANEL } from '../gp-params.ts'
import { ReviewSopButton } from './ReviewSopButton.tsx'

/** Cordis 插件名（与 host 半一致）。 */
export const name = 'paperpilot'

/** 需要 web client 的服务：slots（挂按钮）+ layout（开右栏）
 *  + `sidebarRight` / `sidebarRightTabs`（原生页签）。
 *  Cordis ctx 是代理：未在 inject 声明的服务一访问就抛，故用到的必须在此声明。 */
export const inject = ['slots', 'layout', 'sidebarRight', 'sidebarRightTabs']

/** ⭐ §7.3.3 定案：逐服务一个 `kind` / 一个 `id`（契约拒绝重复 id 与同 tier 同 kind 二次注册）。 */
const TAB_KIND = 'mecha:GP'
const TAB_ID = 'mecha-panel:GP'

export async function apply(ctx: Context): Promise<void> {
  await ctx.effect(() => {
    // ⭐ 一次调用管全部：参数**跟着这次注册走**（标题闭包捕获自己那份、正文以显式 prop
    // 收到自己那份）；头部「◈ 监控」按钮由框架注册**唯一一个**。
    // ⚠ 参数在这里**交出**（不再顶层 `configurePanel`）—— helper 内部会用
    //   `openPanelConfig` 校验并绑定（字段留空 ⇒ 当场抛，响亮失败）。
    const disposePanel = mountClientPanel(ctx as never, {
      panelConfig: GP_PANEL,
      kind: TAB_KIND,
      id: TAB_ID,
      body: MonitorTabBody,
      titlePrefix: '◈ 监控 · ',
    })

    // ⭐ **本仓自己的**按钮（自定义 UI 那条路，**不是**监控面板那一份）：
    // 「☀ 评审今日」（D1）：把固定评审 SOP 一键复制进剪贴板（零宿主 API 假设）。
    const disposeSopBtn = ctx.slots.inject('conversation.session.header.actions', () =>
      ctx.slots.register(
        { name: 'conversation.session.header.actions', id: 'pp-review-sop', order: 202 },
        ReviewSopButton,
      ))

    return () => {
      try { disposeSopBtn?.() } catch { /* ignore */ }
      disposePanel()
    }
  }, 'paperpilot: monitor tab')
}
