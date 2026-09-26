/**
 * 会话头部动作：PaperPilot 面板的**切换开关**（不是弹窗）。
 *
 * 点一下把 PaperPilot Web（今日简报/论文库）挂进 dsh 右栏、AI 对话回中栏全高；再点切回
 * 原版 dsh。状态放在模块级 mode-store，与右栏 iframe 共享（两者 scope 不同，只能模块级桥接）。
 * 自包含、无外部 CSS 依赖。
 */
import { useSyncExternalStore } from 'react'
import type { PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import { getPanelMode, subscribePanelMode, togglePanelMode } from './mode-store.ts'

type Props = PropsRuntime<'conversation.session.header.actions'>

export function BriefingPanel(_props: Props) {
  const on = useSyncExternalStore(subscribePanelMode, getPanelMode)
  return (
    <button
      type="button"
      onClick={() => togglePanelMode()}
      data-pp-panel-toggle={on ? 'on' : 'off'}
      title={on
        ? '收起 PaperPilot 面板，回到原版 dsh 布局'
        : '在右栏打开 PaperPilot：今日简报 / 论文库 / 设置（arXiv 每日文献情报）'}
    >
      {on ? '📄 收起简报' : '📄 简报'}
    </button>
  )
}
