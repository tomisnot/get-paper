/**
 * 会话头部动作：**◈ 监控**（AI 干了什么 = mecha cockpit 的原生面板）。
 *
 * 〔第 5 批简化〕「📄 简报」iframe 链退役后，这是右栏**唯一**的面板入口，
 * 不再有"两面板互斥"的标题分支（mode-store 依赖随之消失）。
 *
 * 形态照 EL 的 `buttons.tsx`：按钮不自己开合，只调注入的 `openCockpit`——
 * "开右栏 + 开页签"是宿主侧动作（实现见 `client/index.ts`）。
 */
import type { PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'

type Props = PropsRuntime<'conversation.session.header.actions'>

/** 注入面：宿主（`client/index.ts`）给的动作。 */
export interface MonitorInjected {
  /** 开右栏 → 打开监控页签。 */
  openCockpit: () => void
}

export function MonitorButton(props: Props & Partial<MonitorInjected>) {
  const open = props.openCockpit
  return (
    <button
      type="button"
      onClick={() => open?.()}
      data-pp-monitor-toggle="on"
      title="打开操作审计（mecha cockpit：写权模式 + 命令审计 + 配置态，3s 自刷新）"
    >
      ◈ 监控
    </button>
  )
}
