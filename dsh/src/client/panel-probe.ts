/**
 * **两跳可达性探测**（client 半，React 无关、可 Node 直接单测）。
 *
 * ## 归属
 *
 * ⚠ 服务的是 **📄 简报（可视化面）iframe** —— 用户点名保留的那一个。
 * **AI 监控面板改走共享资产的原生 tab**（不是 iframe、不走两跳）⇒ 与它无关。
 *
 * ## 为什么必须有它（共享资产点名的一条**项目侧义务**）
 *
 * 简报面板是 **iframe（两跳）**：外层 dsh 页 → 内层 PaperPilot Web。两跳**跨源**
 * （端口不同）⇒ 外层**读不到**内层的 DOM，也就**无法**知道"内层画了什么"。
 * 资产管的是**数据层**（`panelState` 让"没数据"没法冒充成功）；而
 * **"外层 200、内层空白"**是**渲染/挂载层**的病，**资产结构上管不到**，只能由项目兜住。
 *
 * 本模块兜的就是其中"**内层服务压根没在听**"这一半：挂之前先探一次，
 * 探不到就把失败**呈现成可读文本**，而不是挂一个永远空白的 iframe。
 *
 * ⚠ **诚实边界**（不许假装覆盖）：探测用 `mode:'no-cors'`，**只回答"那个地址上有人讲 HTTP 吗"**：
 *   * 解析 ⇒ 有响应（**任何**状态码都算有，`no-cors` 下读不到状态与正文）；
 *   * 拒绝 ⇒ TCP/HTTP 层连不上（Web 没起、端口被别的东西占了）。
 * 它**不能**证明内层页渲染出了内容 ⇒ "内层页面自己永远不空白"这条由**内层页**负责
 * （Web 各页在没有 mecha 栈时如实渲染，不是空白）。
 */
export type DoFetch = typeof fetch

/**
 * 探一次地址可达性。**绝不抛**：任何异常都折成 `false`（调用方按"不可达"呈现）。
 *
 * 用 `no-cors` 是刻意的：本探测**不需要读正文**，跨源下读正文需要 CORS 头，
 * 而"没配 CORS 但服务活着"会被误判成不可达（那会把真因反过来吃掉）。
 */
export async function probeReachable(base: string, doFetch: DoFetch = fetch): Promise<boolean> {
  try {
    await doFetch(`${base.replace(/\/+$/, '')}/healthz`, { mode: 'no-cors', cache: 'no-store' })
    return true
  } catch {
    return false
  }
}
