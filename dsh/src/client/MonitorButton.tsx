/**
 * 会话头部动作：**◈ 监控**（AI 干了什么 = mecha cockpit 的原生面板）。
 *
 * 与「📄 简报」的区别（**这不是同一种东西，所以两者互斥**）：
 *   · **◈ 监控** = dsh **原生页签**（`sidebarRightTabs` 注册的那种），布局与开合由 dsh 自管；
 *   · **📄 简报** = 往 `[data-rightbar-col]` 塞 iframe + CSS 重排三列（本项目自有）。
 * ⇒ 同时开会打架，规则是**互斥**：开监控先收起简报面板，点简报先关掉监控页签
 *   （实现都在 `client/index.ts`；本组件只管"点了要什么"）。
 *
 * 形态照 EL 的 `buttons.tsx`（用户点名"几乎完全复用 EL，布局也是"）：按钮不自己开合，
 * 只调注入的 `openCockpit`——"开右栏 + 开页签"是宿主侧动作。
 */
import { useSyncExternalStore } from 'react'
import type { PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import { getPanelMode, subscribePanelMode } from './mode-store.ts'

type Props = PropsRuntime<'conversation.session.header.actions'>

/** 注入面：宿主（`client/index.ts`）给的动作。 */
export interface MonitorInjected {
  /** 关掉简报面板（互斥）→ 开右栏 → 打开监控页签。 */
  openCockpit: () => void
}

export function MonitorButton(props: Props & Partial<MonitorInjected>) {
  // 「简报面板开着」时，标题里说清"点它会先收起它"（互斥是设计，不是 bug）。
  const briefingOn = useSyncExternalStore(subscribePanelMode, getPanelMode)
  const open = props.openCockpit
  return (
    <button
      type="button"
      onClick={() => open?.()}
      data-pp-monitor-toggle="on"
      title={briefingOn
        ? '打开操作审计（mecha cockpit：写权模式 + 命令审计 + 配置态）；会先收起「📄 简报」面板'
        : '打开操作审计（mecha cockpit：写权模式 + 命令审计 + 配置态，3s 自刷新）'}
    >
      ◈ 监控
    </button>
  )
}
