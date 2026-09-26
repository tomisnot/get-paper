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
 * ⚠ client 半经 tsdown 打成 CJS + `__ModuleLoader__.load` 包装；改本目录源码后须
 * `npm run bundle` 并重启 dsh 才生效。
 */
import type { Context } from '@deepseek-ai/cordis'
import { BriefingPanel } from './BriefingPanel.tsx'
import { PANEL_MODE_CSS } from './panel-mode-css.ts'
import { getPanelMode, subscribePanelMode } from './mode-store.ts'
import { readBootstrap } from './bootstrap.ts'

/** Cordis 插件名（与 host 半一致）。 */
export const name = 'paperpilot'

/** 需要 web client 的服务：slots（挂按钮）+ layout（开/关右栏、关左栏）。
 *  Cordis ctx 是代理：未在 inject 声明的服务一访问就抛
 *  "cannot get property X without inject"，故 layout 必须在此声明。 */
export const inject = ['slots', 'layout']

export async function apply(ctx: Context): Promise<void> {
  await ctx.effect(() => {
    // 注入面板模式 CSS（data-plugin-css 标记；卸载即移除）
    const style = document.createElement('style')
    style.setAttribute('data-plugin-css', 'pp-panel-mode')
    style.textContent = PANEL_MODE_CSS
    document.head.appendChild(style)

    // 会话头部：PaperPilot 面板切换按钮
    const disposeBtn = ctx.slots.inject('conversation.session.header.actions', () =>
      ctx.slots.register(
        { name: 'conversation.session.header.actions', id: 'pp-briefing', order: 200 },
        BriefingPanel,
      ))

    // ---- 模式副作用：右栏=PaperPilot 面板 + 左栏自动关 + iframe 挂右栏列 ----
    let frame: HTMLIFrameElement | null = null
    let weClosedSidebar = false

    const mountFrame = (): void => {
      const host = document.querySelector('[data-rightbar-col]')
      if (!host || frame) return
      frame = document.createElement('iframe')
      frame.className = 'pp-panel-frame'
      const url = readBootstrap()?.webUrl
      if (url) frame.src = url
      frame.title = 'PaperPilot（arXiv 每日简报）'
      host.appendChild(frame)
    }
    const unmountFrame = (): void => {
      if (frame) { frame.remove(); frame = null }
    }

    const unsub = subscribePanelMode(() => {
      if (getPanelMode()) {
        // 运行版 dsh(0.1.5-rc.2) 的 ctx.layout 是 openRightbar/closeRightbar（非 openDetails）；
        // 全部可选链：API 缺失时降级不崩整站。openRightbar(true,false)=预留右栏 grid track。
        ctx.layout?.openRightbar?.(true, false)
        // 左栏若开着则关掉（记住是我们关的，OFF 时复原）
        const fr = document.querySelector('[data-pp-frame]')
        if (fr && !fr.hasAttribute('data-sidebar-collapsed')) {
          ctx.layout?.toggleSidebar?.()
          weClosedSidebar = true
        }
        mountFrame()
      } else {
        ctx.layout?.closeRightbar?.()
        if (weClosedSidebar) { ctx.layout?.toggleSidebar?.(); weClosedSidebar = false }
        unmountFrame()
      }
    })

    return () => {
      unsub()
      unmountFrame()
      try { disposeBtn?.() } catch { /* ignore */ }
      style.remove()
      document.documentElement.removeAttribute('data-pp-panel')
    }
  }, 'paperpilot: briefing panel')
}
