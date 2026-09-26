/**
 * paperpilot dsh 插件 —— **CLIENT 入口**（浏览器侧 Cordis 插件）。
 *
 * PaperPilot 主界面 = dsh **右侧栏**：点 📄简报 → `openRightbar` 打开右栏 + 把
 * PaperPilot Web iframe 挂进右栏列（`[data-rightbar-col]`）+ 自动关左栏；对话回中栏全高
 * （宽度随右栏打开自动收窄）。再点 → closeRightbar + 复原左栏 + 移除 iframe。
 *
 * 贡献：① 会话头部切换按钮（`conversation.session.header.actions`）；② 注入 CSS
 * （`<style data-plugin-css>`，仅 `html[data-pp-panel="on"]` 生效）。
 * 跨 scope（按钮 session / iframe root）的模式状态由模块级 mode-store 桥接；
 * 注册与副作用都经 `ctx.effect` 可逆。
 *
 * ## 地址**不再**来自注入的 bootstrap（2026-09-26 迁移）
 *
 * 旧形态 `readBootstrap()?.webUrl || ''`：地址是 host 半注入的**静态默认**
 * `http://127.0.0.1:8080/`（`src/index.ts` 的 `config.webUrl`），与 `settings.yaml` 的
 * `web.port` **各写一份**——换端口即漂移；注入缺失时 src 还会退化成相对 `monitor`
 * （按 dsh 自己的域解析）或空串（iframe 永不设 src = 纯白）。
 *
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
import { MonitorButton } from './MonitorButton.tsx'
import { PANEL_MODE_CSS } from './panel-mode-css.ts'
import { getPanelMode, getPanelPath, subscribePanelMode } from './mode-store.ts'
import { PANEL_CONFIG } from '../panel/panel-config.ts'
import { assertNever, fetchMonitorBase } from '../panel/monitor-client.ts'
import { probeReachable } from './panel-probe.ts'
import { renderPanel, type PanelRender } from './panel-state.ts'

/** Cordis 插件名（与 host 半一致）。 */
export const name = 'paperpilot'

/** 需要 web client 的服务：slots（挂按钮）+ layout（开/关右栏、关左栏）。
 *  Cordis ctx 是代理：未在 inject 声明的服务一访问就抛
 *  "cannot get property X without inject"，故 layout 必须在此声明。 */
export const inject = ['slots', 'layout']

/** 错误态自动重试间隔（展示刷新，非正确性依赖）。 */
const RETRY_MS = 2500

export async function apply(ctx: Context): Promise<void> {
  await ctx.effect(() => {
    // 注入面板模式 CSS（data-plugin-css 标记；卸载即移除）
    const style = document.createElement('style')
    style.setAttribute('data-plugin-css', 'pp-panel-mode')
    style.textContent = PANEL_MODE_CSS
    document.head.appendChild(style)

    // 会话头部：📄简报按钮（切到 Web 根）+ ◈监控按钮（切到 /monitor 操作审计）
    const disposeBtn = ctx.slots.inject('conversation.session.header.actions', () =>
      ctx.slots.register(
        { name: 'conversation.session.header.actions', id: 'pp-briefing', order: 200 },
        BriefingPanel,
      ))
    const disposeMonitorBtn = ctx.slots.inject('conversation.session.header.actions', () =>
      ctx.slots.register(
        { name: 'conversation.session.header.actions', id: 'pp-monitor', order: 201 },
        MonitorButton,
      ))

    // ---- 模式副作用：右栏=PaperPilot 面板（简报或监控）+ 左栏自动关 + iframe 挂右栏列 ----
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
        view: getPanelPath() === 'monitor' ? 'monitor' : '',
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
      try { disposeMonitorBtn?.() } catch { /* ignore */ }
      try { disposeBtn?.() } catch { /* ignore */ }
      style.remove()
      document.documentElement.removeAttribute('data-pp-panel')
    }
  }, 'paperpilot: briefing + monitor panel')
}
