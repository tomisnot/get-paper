/**
 * PaperPilot 面板模式的极小模块级 store。
 *
 * 为什么要模块级：切换按钮在 `conversation.session.header.actions`（scope=session），
 * iframe 挂在 `[data-rightbar-col]`（scope=root）——slot 的 store 是逐注册绑定的，跨这两个
 * 不同 scope 的组件共享不了，故用一个模块级单例 observable 桥接（配 useSyncExternalStore）。
 *
 * 副作用：翻转时给 `<html>` 打/去 `data-pp-panel="on"`（注入 CSS 的总开关），并按稳定
 * 锚点给 dsh 的三列打 `data-pp-*` 标记——列类名是 CSS Modules hash、CSS 选不中，只能运行时
 * 靠 `[data-rightbar-col]` 的兄弟关系定位后打标。
 */

type Listener = () => void

let panelMode = false
const listeners = new Set<Listener>()
let observer: MutationObserver | null = null

/** 当前是否处于 PaperPilot 面板模式。 */
export function getPanelMode(): boolean {
  return panelMode
}

/** useSyncExternalStore 的 subscribe：返回退订函数。 */
export function subscribePanelMode(fn: Listener): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

/** 翻转面板模式（头部按钮调用）。 */
export function togglePanelMode(): void {
  setPanelMode(!panelMode)
}

/** 设定面板模式；变化时落 DOM 副作用并通知订阅者。 */
export function setPanelMode(on: boolean): void {
  if (on === panelMode) return
  panelMode = on
  applyDom(on)
  for (const fn of listeners) fn()
}

function applyDom(on: boolean): void {
  const root = document.documentElement
  if (on) {
    root.setAttribute('data-pp-panel', 'on')
    markColumns()
    // dsh 重挂载 frame 会丢列标记 → 观察 DOM childList 变化补打（rAF 节流）。只观察
    // childList、不观察 attributes，故 markColumns 的 setAttribute 不会回环触发自己。
    if (!observer) {
      let scheduled = false
      observer = new MutationObserver(() => {
        if (scheduled || !panelMode) return
        scheduled = true
        requestAnimationFrame(() => {
          scheduled = false
          if (panelMode) markColumns()
        })
      })
      observer.observe(document.body, { childList: true, subtree: true })
    }
  } else {
    root.removeAttribute('data-pp-panel')
    observer?.disconnect()
    observer = null
  }
}

/**
 * 按稳定锚点给 frame 的三列打 data-pp-* 标记（幂等，可重复调用）。
 * AppFrame 的 DOM 子元素顺序：sidebarCol | centerCol(main→conversation) | rightbarCol
 *   ([data-rightbar-col]) | overlayLayer([data-shell-overlay]) | 拖拽把手…
 * 故从 rightbarCol 往前两跳即得 centerCol / sidebarCol。
 */
export function markColumns(): void {
  const overlay = document.querySelector('[data-shell-overlay]')
  const frame = overlay?.parentElement ?? null
  if (!frame) return
  frame.setAttribute('data-pp-frame', '')
  const rightbar = frame.querySelector(':scope > [data-rightbar-col]')
  const center = rightbar?.previousElementSibling ?? null
  const sidebar = center?.previousElementSibling ?? null
  if (center) center.setAttribute('data-pp-center', '')
  if (sidebar) sidebar.setAttribute('data-pp-sidebar', '')
}
