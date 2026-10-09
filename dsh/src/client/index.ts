/**
 * paperpilot dsh 插件 —— **CLIENT 入口**（浏览器侧 Cordis 插件）。
 *
 * ## ⭐ 2026-10 阶段②：监控面板那一半**不再由本仓写**，只留"接上框架 + 自定义 UI"
 *
 * 框架那份插件（`@mecha/dsh-panel/panel/auto.ts`）读框架起 dsh 时按**声明**
 * （`mecha_site.py` 的 `Service.monitor`）生成的那份参数，**按服务挂 N 个 tab**
 * （三家各挂自己那一份），头部按钮**恒为 1 个**。
 * ⚠ GP 与另两家不同的一点：它**自己有自定义 UI**（下面的「☀ 评审今日」按钮）⇒
 * 它的 `apply` 必须是**自己的**（`export { apply } from …` 那条一行写法在这里不适用），
 * 于是本文件**多一句** `await applyMechaPanels(ctx)` —— 那是"自定义 UI 保留为可选"
 * （§7.3.3）的代价，**不是**监控面板那部分没上提。
 *
 * ## 保留的东西（用户明确要留）
 *
 * - 「☀ 评审今日」（`ReviewSopButton`）：一键复制固定评审 SOP。⭐ 它属于**自定义 UI**，
 *   与监控面板无关 ⇒ 阶段② **不删**。
 *
 * ## 已删掉的东西（别往回加）
 *
 * - 会话头部「◈ 监控」按钮 + `sidebarRightTabs`/`sidebar.right.pane.tab` 的手写注册：
 *   阶段① 起归框架；⚠ 原先 `title: () => panelConfig().title` 是**渲染时求值**的 thunk
 *   ⇒ 三家装进同一个 dsh 时三个页签的标题与取数路由**全落到"最后注册那一份"**。
 * - 〔历史〕「📄 简报」iframe 链：2026-09-26 第 5 批用户裁决**整体退役**
 *   （阅读面 = PaperPilot 自家 Web，dsh 侧栏只留监控页签）。
 *
 * ## 面板参数的家在哪
 *
 * ⭐ **在声明**（`mecha_site.py` 的 `monitor=MonitorSpec(...)`）。⚠ `dsh/src/gp-params.ts`
 * 的 `GP_PANEL` **仍在**，因为 **host 半**那条"监控地址路由"仍由本仓注册
 * （§7.3.8 末段如实登记为下一步）；两处必须同值，由 `test/panel.test.ts` 的对拍钉住。
 *
 * ⚠ client 半经 tsdown 打成 CJS + `__ModuleLoader__.load` 包装；改本目录源码后须
 * `npm run bundle` 并重启 dsh 才生效。
 */
import type { Context } from '@deepseek-ai/cordis'
import { applyMechaPanels } from '@mecha/dsh-panel/panel/auto.ts'
import { ReviewSopButton } from './ReviewSopButton.tsx'

/** Cordis 插件名（与 host 半一致）。 */
export const name = 'paperpilot'

/** 需要 web client 的服务：slots（挂按钮）+ layout（开右栏）
 *  + `sidebarRight` / `sidebarRightTabs`（原生页签，框架挂 tab 时要用）。
 *  Cordis ctx 是代理：未在 inject 声明的服务一访问就抛，故用到的必须在此声明。 */
export const inject = ['slots', 'layout', 'sidebarRight', 'sidebarRightTabs']

/** 会话头部动作槽位（本仓自己那个按钮用的；监控按钮不在这里了）。 */
const HEADER_ACTIONS_SLOT = 'conversation.session.header.actions'
/** 本仓那个按钮的槽位 id（⚠ 与框架的 `mecha-panel` 不撞）。 */
const SOP_BUTTON_ID = 'pp-review-sop'

export async function apply(ctx: Context): Promise<void> {
  await ctx.effect(async () => {
    // ⭐ 监控面板：**框架的活**（按声明挂 N 个 tab；正文/标题各读自己那份）。
    // ⚠ 取不到参数时它自己**出声**（logger.error）且不挂 tab —— 不抛 ⇒ 下面的自定义
    //   按钮照样在（把"面板没接上"升级成"项目在 dsh 里没了"是更坏的结果）。
    const disposePanel = await applyMechaPanels(ctx as never)

    // ⭐ **本仓自己的**按钮（自定义 UI，**不是**监控面板那一份）：
    // 「☀ 评审今日」（D1）：把固定评审 SOP 一键复制进剪贴板（零宿主 API 假设）。
    const disposeSopBtn = ctx.slots.inject(HEADER_ACTIONS_SLOT, () =>
      ctx.slots.register(
        { name: HEADER_ACTIONS_SLOT, id: SOP_BUTTON_ID, order: 202 },
        ReviewSopButton,
      ))

    return () => {
      // ⚠ **不吞异常**：收尾里抛的要带到调用方（资产的 helper 同一条口径）。
      //   先收自己那个按钮，再收框架挂的那些 tab（与注册顺序相反）。
      disposeSopBtn?.()
      disposePanel()
    }
  }, 'paperpilot: monitor tabs by declaration + 评审按钮（自定义 UI）')
}
