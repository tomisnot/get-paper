/**
 * paperpilot dsh 插件 —— **CLIENT 入口**（浏览器侧 Cordis 插件）。
 *
 * PaperPilot 主界面 = dsh **右侧栏**：点 📄简报 → `openRightbar` 打开右栏 + 把
 * PaperPilot Web iframe 挂进右栏列（`[data-rightbar-col]`）+ 自动关左栏；对话回中栏全高
 * （宽度随右栏打开自动收窄）。再点 → closeRightbar + 复原左栏 + 移除 iframe。
 *
 * 贡献：① 会话头部 📄简报 按钮（`conversation.session.header.actions`）；② 注入 CSS
 * （`<style data-plugin-css>`，仅 `html[data-pp-panel="on"]` 生效）+ 简报 iframe 的挂载/取址。
 * 跨 scope（按钮 session / iframe root）的模式状态由模块级 mode-store 桥接；
 * 注册与副作用都经 `ctx.effect` 可逆。
 *
 * ## 两个面（**两种形态，所以互斥**）
 *
 * * **📄 简报**（可视化面）= **本文件的 iframe 链**：`openRightbar` 打开右栏 + 把 PaperPilot
 *   Web 挂进右栏列（`[data-rightbar-col]`）+ 自动关左栏；再点 → 复原。地址走同源只读路由
 *   （见下），判定在 `panel-state.ts`，两跳可达性在 `panel-probe.ts`。
 * * **◈ 监控**（AI 干了什么）= **共享资产 `dsh-panel/` 的原生页签**：数据层 `panel-data.ts`、
 *   呈现 `panel-view.ts`、壳 `MonitorTabBody.tsx`（**本项目只提供参数块**）。注册形状抄 EL。
 *
 * ⚠ **两面板共用右栏 ⇒ 必须互斥**（D1 裁决）：开监控先 `setPanelMode(false)` 收起 iframe；
 * 打开简报先 `sidebarRight.closeTab(...)` 关掉页签。**两种东西同时开会打架，这是设计不是缺陷。**
 *
 * 贡献：① 会话头部「📄 简报」「◈ 监控」两个按钮（`conversation.session.header.actions`）；
 * ② 右栏页签类型 + body（`sidebarRightTabs` / `sidebar.right.pane.tab`）；③ 简报面板的 CSS
 * 与 iframe 挂载/取址。跨 scope 的模式状态由模块级 mode-store 桥接；注册与副作用经 `ctx.effect` 可逆。
 *
 * ## 地址**不再**来自注入的 bootstrap
 *
 * 旧形态 `readBootstrap()?.webUrl || ''`：地址是 host 半注入的**静态默认**
 * `http://127.0.0.1:8080/`（与 `settings.yaml` 的 `web.port` **各写一份**）——换端口即漂移；
 * 注入缺失时 src 还会退化成相对路径（按 dsh 自己的域解析）或空串（iframe 永不设 src = 纯白）。
 * 现形态：地址经**同源只读路由** `PANEL_CONFIG.ROUTE_PATH` **现取**（host 半读项目根
 * `PANEL_CONFIG.PORT_FILE` 里的裸端口，**绝不回落默认端口**）。判定收进纯函数
 * `renderPanel`（`panel-state.ts`）⇒ **"要么可用地址、要么可读错误"，没有第三态**；
 * 错误态还会**自动重试**（Web 比 dsh 起得慢是常态）。
 *
 * ⚠ client 半经 tsdown 打成 CJS + `__ModuleLoader__.load` 包装；改本目录源码后须
 * `npm run bundle` 并重启 dsh 才生效。
 */
import type { Context } from '@deepseek-ai/cordis'
import { BriefingPanel } from './BriefingPanel.tsx'
import { PANEL_MODE_CSS } from './panel-mode-css.ts'
import { getPanelMode, setPanelMode, subscribePanelMode } from './mode-store.ts'
import { PANEL_CONFIG } from '../panel/panel-config.ts'
import { assertNever, fetchMonitorBase } from '../panel/monitor-client.ts'
import { MonitorTabBody } from '../panel/MonitorTabBody.tsx'
import { MonitorButton, type MonitorInjected } from './MonitorButton.tsx'
import { probeReachable } from './panel-probe.ts'
import { renderPanel, type PanelRender } from './panel-state.ts'

/** Cordis 插件名（与 host 半一致）。 */
export const name = 'paperpilot'

/** 需要 web client 的服务：slots（挂按钮）+ layout（开/关右栏、关左栏）
 *  + `sidebarRight` / `sidebarRightTabs`（**监控面板走 dsh 原生页签**）。
 *  Cordis ctx 是代理：未在 inject 声明的服务一访问就抛
 *  "cannot get property X without inject"，故用到的必须在此声明。
 *  ⚠ 与 EL 同款**顶层 inject**（D2 裁决：保持与参考同形，"几乎完全复用 EL"是目标）。
 *  已知隐患（已记账、另批一次修两家）：**缺任一服务 ⇒ 整个 client 插件不激活**
 *  （面板与简报按钮一起消失），而不是"只少一个能力"。 */
export const inject = ['slots', 'layout', 'sidebarRight', 'sidebarRightTabs']

/** 监控面板的右栏页签 id；`kind` 与 `sidebar.right.pane.tab` 的 key **必须一致**。 */
const MONITOR_TAB_ID = 'pp-monitor'

/** 错误态自动重试间隔（展示刷新，非正确性依赖）。 */
const RETRY_MS = 2500

export async function apply(ctx: Context): Promise<void> {
  await ctx.effect(() => {
    // 注入面板模式 CSS（data-plugin-css 标记；卸载即移除）
    const style = document.createElement('style')
    style.setAttribute('data-plugin-css', 'pp-panel-mode')
    style.textContent = PANEL_MODE_CSS
    document.head.appendChild(style)

    // 会话头部：📄简报按钮（把 PaperPilot Web 挂进右栏）
    const disposeBtn = ctx.slots.inject('conversation.session.header.actions', () =>
      ctx.slots.register(
        { name: 'conversation.session.header.actions', id: 'pp-briefing', order: 200 },
        BriefingPanel,
      ))

    // ---- ⭐ AI 监控 = **共享资产的原生页签**（抄 EL 的注册形状；布局在 panel-view.ts 里） ----
    // 「◈ 监控」按钮：**先收起简报面板**（D1 互斥）→ 开右栏 → 打开我们的页签。
    // 按钮不自己开合（宿主侧动作），与 EL 的 `buttons.tsx` 同形。
    const disposeMonitorBtn = ctx.slots.inject('conversation.session.header.actions', () =>
      ctx.slots.register(
        { name: 'conversation.session.header.actions', id: MONITOR_TAB_ID, order: 201,
          label: '◈ 监控',
          inject: (): MonitorInjected => ({
            openCockpit: () => {
              setPanelMode(false)                       // D1：互斥——先把简报 iframe 收掉
              ctx.layout?.openRightbar?.(true, false)
              try { ctx.sidebarRight?.openTab?.(MONITOR_TAB_ID) } catch { /* 降级：右栏开了但没切页签 */ }
            },
          }) } as never,
        MonitorButton,
      ))

    // 页签类型 + body：**数据/呈现全部来自共享资产**（`panel-data.ts` / `panel-view.ts` /
    // `MonitorTabBody.tsx`），本项目只提供参数块（路由 / 端口文件 / 标题）。零 react 判据落在
    // 资产那四份自测里；`.tsx` 壳只过 typecheck。
    const releaseTabType = ctx.sidebarRightTabs?.register?.({
      id: MONITOR_TAB_ID, kind: MONITOR_TAB_ID, title: () => PANEL_CONFIG.TITLE,
    }) ?? null
    const disposeTabBody = ctx.slots.inject('sidebar.right.pane.tab', () =>
      ctx.slots.register(
        { name: 'sidebar.right.pane.tab', key: MONITOR_TAB_ID } as never,
        MonitorTabBody as never,
      ))

    // ---- 模式副作用：右栏=PaperPilot 简报 iframe + 左栏自动关 ----
    let frame: HTMLIFrameElement | null = null
    let errorBox: HTMLElement | null = null
    let overlay: HTMLElement | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let paintToken = 0            // 异步取址的竞态令牌：只有最新一次能落 DOM
    let weClosedSidebar = false
    let wasOn = false

    const hostEl = (): Element | null => document.querySelector('[data-rightbar-col]')

    const ensureFrame = (host: Element): HTMLIFrameElement => {
      if (!frame) {
        frame = document.createElement('iframe')
        frame.className = 'pp-panel-frame'
        frame.title = `${PANEL_CONFIG.TITLE}（PaperPilot）`
        host.appendChild(frame)
      } else if (frame.parentElement !== host) {
        host.appendChild(frame)     // dsh 重挂右栏列 ⇒ 把 iframe 跟着搬过去
      }
      return frame
    }

    const showError = (message: string): void => {
      // 有话直说：能挂进右栏就挂那儿；连容器都没有就贴一张固定定位的说明卡（**绝不留白**）。
      if (frame) { frame.remove(); frame = null }   // 不给"旧内容 + 错误"并存
      const host = hostEl()
      if (host) {
        if (!errorBox) {
          errorBox = document.createElement('div')
          errorBox.className = 'pp-panel-error'
          host.appendChild(errorBox)
        } else if (errorBox.parentElement !== host) {
          host.appendChild(errorBox)
        }
        errorBox.textContent = message
        return
      }
      if (!overlay) {
        overlay = document.createElement('div')
        overlay.className = 'pp-panel-error pp-panel-error-overlay'
        document.body.appendChild(overlay)
      }
      overlay.textContent = message
      ctx.logger?.warn?.(`[paperpilot] 面板无法挂载：${message}`)
    }

    const clearError = (): void => {
      if (errorBox) { errorBox.remove(); errorBox = null }
      if (overlay) { overlay.remove(); overlay = null }
    }

    const paint = (render: PanelRender): void => {
      // 判别联合 + `assertNever`：**没有"什么都不画"的分支**（资产同款纪律）。
      switch (render.kind) {
        case 'error':
          showError(render.message)
          scheduleRetry()
          return
        case 'ready': {
          clearError()
          const host = hostEl()
          if (!host) {
            // 判定时容器还在、落 DOM 时没了 ⇒ 仍按"可读错误"处理（不静默）
            showError('面板地址已取到，但右栏容器（[data-rightbar-col]）不在——请收起再打开面板。')
            scheduleRetry()
            return
          }
          const el = ensureFrame(host)
          if (el.getAttribute('src') !== render.src) el.src = render.src
          return
        }
        default:
          // 新增 kind 而忘了处理 ⇒ 编译不过；运行时真收到 ⇒ 抛，绝不静默画空气
          assertNever(render)
      }
    }

    const scheduleRetry = (): void => {
      if (retryTimer) return
      retryTimer = setTimeout(() => {
        retryTimer = null
        if (getPanelMode()) void refresh()
      }, RETRY_MS)
    }

    /**
     * 取址 → 探可达 → 判定 → 落 DOM（**每次现取**：端口漂移下一拍自愈）。
     *
     * 取址走**共享资产的 `fetchMonitorBase`**（`doFetch` 是它真用的注入缝）；探可达见
     * `panel-probe.ts`（"两跳必须可诊断"这条项目侧义务）。
     */
    const refresh = async (): Promise<void> => {
      const token = ++paintToken
      const address = await fetchMonitorBase(fetch, PANEL_CONFIG.ROUTE_PATH)
      const reachable = address.ok ? await probeReachable(address.data) : false
      if (token !== paintToken || !getPanelMode()) return    // 已被更新的取址/已收起取代
      // 一切错误态都值得重试（见 panel-state.ts 末尾的 R17 自查：不留"不可重试"的假缝）
      paint(renderPanel({
        address,
        reachable,
        hasHost: Boolean(hostEl()),
        routePath: PANEL_CONFIG.ROUTE_PATH,
        portFile: PANEL_CONFIG.PORT_FILE,
      }))
    }

    const unmountPanel = (): void => {
      paintToken++                       // 作废在途取址
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
      if (frame) { frame.remove(); frame = null }
      clearError()
    }

    const unsub = subscribePanelMode(() => {
      const on = getPanelMode()
      if (on) {
        // D1 互斥的另一半：**打开简报面板时先关掉监控页签**——两者是两种东西
        // （iframe + CSS 重排三列 vs dsh 原生页签），同时开会打架。
        try { ctx.sidebarRight?.closeTab?.(MONITOR_TAB_ID) } catch { /* 降级 */ }
        // 运行版 dsh(0.1.5-rc.2) 的 ctx.layout 是 openRightbar/closeRightbar（非 openDetails）；
        // 全部可选链：API 缺失时降级不崩整站。openRightbar(true,false)=预留右栏 grid track。
        ctx.layout?.openRightbar?.(true, false)
        // 左栏只在 关→开 的转场收一次（切视图时不重复 toggle）
        if (!wasOn) {
          const fr = document.querySelector('[data-pp-frame]')
          if (fr && !fr.hasAttribute('data-sidebar-collapsed')) {
            ctx.layout?.toggleSidebar?.()
            weClosedSidebar = true
          }
        }
        void refresh()
      } else {
        ctx.layout?.closeRightbar?.()
        if (weClosedSidebar) { ctx.layout?.toggleSidebar?.(); weClosedSidebar = false }
        unmountPanel()
      }
      wasOn = on
    })

    return () => {
      unsub()
      unmountPanel()
      if (disposeTabBody) { try { disposeTabBody() } catch { /* ignore */ } }
      if (typeof releaseTabType === 'function') { try { releaseTabType() } catch { /* ignore */ } }
      try { disposeMonitorBtn?.() } catch { /* ignore */ }
      try { disposeBtn?.() } catch { /* ignore */ }
      style.remove()
      document.documentElement.removeAttribute('data-pp-panel')
    }
  }, 'paperpilot: briefing iframe + monitor tab')
}
