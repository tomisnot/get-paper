/**
 * 会话头部动作：**📄 简报**（可视化面）的切换开关。
 *
 * 点一下把 PaperPilot Web（今日简报/论文库/设置）挂进 dsh 右栏、AI 对话回中栏全高；再点切回
 * 原版 dsh。状态放模块级 mode-store，与右栏 iframe 共享（两者 scope 不同，只能模块级桥接）。
 * 自包含、无外部 CSS 依赖。
 *
 * ## ⚠ 与 AI 监控面板的关系（**两面板互斥**，别当成 bug）
 *
 * 「◈ 监控」那块是**另一种东西**：它走共享资产（`dsh-panel/`）的**原生 tab**（dsh 自管页签
 * 与布局）；而本按钮是**往 `[data-rightbar-col]` 塞 iframe + CSS 重排三列**（隐藏左栏、
 * 中栏 fixed 到右上）。两者**同时开会打架** ⇒ 规则是**互斥**：开监控 tab 时先收起本面板
 * （`setPanelMode(false)`），点本按钮时先关掉那个 tab。**现状如此，不是缺陷。**
 * （监控 tab 尚未抄装——等共享资产的面板落地时按这条实现。）
 */
import { useSyncExternalStore } from 'react'
import type { PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import { getPanelMode, subscribePanelMode, togglePanel } from './mode-store.ts'

type Props = PropsRuntime<'conversation.session.header.actions'>

export function BriefingPanel(_props: Props) {
  const on = useSyncExternalStore(subscribePanelMode, getPanelMode)
  return (
    <button
      type="button"
      onClick={() => togglePanel()}
      data-pp-panel-toggle={on ? 'on' : 'off'}
      title={on
        ? '收起 PaperPilot 面板，回到原版 dsh 布局'
        : '在右栏打开 PaperPilot：今日简报 / 论文库 / 设置（arXiv 每日文献情报）'}
    >
      {on ? '📄 收起简报' : '📄 简报'}
    </button>
  )
}
