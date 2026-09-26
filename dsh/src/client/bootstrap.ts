/**
 * 读取 host 经 `webserver/index-inject` 注入的 bootstrap（`globalThis.__PAPERPILOT__`）。
 *
 * host 半在 web 模式把 `{webUrl}` 注进 index.html；client 面板据此 iframe PaperPilot Web。
 * 非 web 模式 / host 半未加载时返回 null（面板不挂 iframe，不崩）。
 */
export interface PaperPilotBootstrap {
  /** PaperPilot Web 面板地址（今日简报/论文库/设置）。 */
  webUrl?: string
}

export function readBootstrap(): PaperPilotBootstrap | null {
  const raw = (globalThis as { __PAPERPILOT__?: unknown }).__PAPERPILOT__
  if (!raw || typeof raw !== 'object') return null
  return raw as PaperPilotBootstrap
}
