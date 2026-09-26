/**
 * 会话头部动作：**◈ 监控** 按钮 —— 在右栏打开 PaperPilot 的操作审计面（mecha cockpit）。
 *
 * 与「📄 简报」按钮共用同一 iframe 机制（mode-store），只是把 iframe 指到 Web 的
 * `/monitor` 页（服务端渲染 mecha Monitor：写权模式 + 命令审计 + 配置态，可被原始证伪）。
 * 两按钮互斥高亮：谁对应当前视图谁亮；再点当前视图则收起。
 */
import { useSyncExternalStore } from 'react'
import type { PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import { getPanelMode, getPanelPath, subscribePanelMode, togglePanel } from './mode-store.ts'

type Props = PropsRuntime<'conversation.session.header.actions'>

export function MonitorButton(_props: Props) {
  const on = useSyncExternalStore(
    subscribePanelMode,
    () => getPanelMode() && getPanelPath() === 'monitor',
  )
  return (
    <button
      type="button"
      onClick={() => togglePanel('monitor')}
      data-pp-monitor-toggle={on ? 'on' : 'off'}
      title={on
        ? '收起操作审计监控'
        : '在右栏打开操作审计（mecha cockpit：写权模式 + 命令审计 + 配置态，5s 自刷新）'}
    >
      {on ? '◈ 收起监控' : '◈ 监控'}
    </button>
  )
}
