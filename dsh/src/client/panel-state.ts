/**
 * **简报 iframe** 的状态判定（纯逻辑、零 DOM）：把"地址解析结果 + 内层可达性 + 容器是否在"
 * 映成**要么一个可用的 iframe 地址、要么一段可读错误**——**不存在"两者都不是"的第三态**。
 *
 * ## 归属（别把它当"监控面板"的零件）
 *
 * ⚠ 本模块服务的是 **📄 简报（可视化面）** 那个 iframe —— 用户点名保留的那一个。
 * **AI 监控面板改走共享资产的原生 tab**（不是 iframe、不走两跳）⇒ 与它无关。
 * （2026-09-26 的删除清单曾把本模块与 `panel-probe.ts` 记成"监控专属、可删"，**那是错的**：
 * 简报 iframe 同样需要"取址 + 可达性 + 可读错误"。）
 *
 * ## 为什么必须是可断言的纯函数
 *
 * 旧形态是 `readBootstrap()?.webUrl || ''` 再 `if (src && …) frame.src = src`：
 * 注入缺失 ⇒ `src` 为空 ⇒ **iframe 永不设 src = 纯白面板且零报错**；还会退化成**相对路径**，
 * 浏览器按 **dsh 自己的域**解析。两类静默都出自同一件事：**"地址"这个返回值允许是空串或
 * 相对串**。收进一个判定函数后，"`ready` ⇒ 非空绝对地址"成为**可以断言的不变量**
 * （判据见 `dsh/test/panel.test.ts`；R8：错误文本也要自证非退化）。
 */
import type { FetchResult } from '../panel/monitor-client.ts'

/** 判定结果：**只有这两种**（"空白"不是一种状态）。 */
export type PanelRender =
  | { kind: 'ready'; src: string }
  | { kind: 'error'; message: string }

/** 判定输入（参数全部注入 ⇒ 纯函数，判据可喂任意组合）。 */
export interface PanelInput {
  /** 地址解析结果（共享 `fetchMonitorBase` 的 `address` 档原样传进来）。 */
  address: FetchResult<string>
  /** 内层 Web 是否应答（`probeReachable` 的结果）；拿不到地址时无意义。 */
  reachable: boolean
  /** 右栏容器（`[data-rightbar-col]`）此刻是否在 DOM 里。 */
  hasHost: boolean
  /** host 半注册的地址路由（报错文案里给人指路用）。 */
  routePath: string
  /** 端口文件名（报错文案里给人指路用）。 */
  portFile: string
}

/**
 * 判定简报面板该渲染什么。
 *
 * 不变量（判据逐条断言）：
 *  1. `ready` ⇒ `src` **非空**且以 `http://` / `https://` 开头（**绝不产相对路径**）；
 *  2. 任何失败 ⇒ `error` 且 `message` **非空**（含失败档位与细节，**不空白**）；
 *  3. 同一输入恒定同结果（纯函数，无隐藏状态）。
 */
export function renderPanel(input: PanelInput): PanelRender {
  if (!input.hasHost) {
    return {
      kind: 'error',
      message:
        '面板无法挂载：找不到右栏容器（[data-rightbar-col]）。' +
        'dsh 的布局变了？本面板不静默留白——请核对 dsh 版本，' +
        '或在会话头部收起/重开面板重试。',
    }
  }
  if (!input.address.ok) {
    return {
      kind: 'error',
      message:
        `面板地址不详（${input.address.failure.tier}）：${input.address.failure.detail}\n` +
        `地址来自同源路由 ${input.routePath}，它读项目根的 ${input.portFile}；` +
        '端口文件不在 = 软件没在跑（paperpilot serve / ai），不是面板坏了。',
    }
  }
  const base = input.address.data.trim().replace(/\/+$/, '')
  if (!base) {
    // 双保险：即便"解析成功但空地址"漏进来，也不许退化成空/相对 src。
    return {
      kind: 'error',
      message: `地址路由返回空地址（${input.routePath}）：不接受空地址去挂 iframe（那会静默留白）。`,
    }
  }
  if (!/^https?:\/\//.test(base)) {
    return {
      kind: 'error',
      message:
        `地址不是绝对地址：${JSON.stringify(base)}（来自 ${input.routePath}）。` +
        '相对地址会被浏览器按 dsh 自己的域解析 ⇒ 拒绝挂载。',
    }
  }
  if (!input.reachable) {
    // ⭐ 两跳边界的可读化：地址是对的，但**那个地址上没人应答** ⇒ 挂上去只会是一片空白。
    return {
      kind: 'error',
      message:
        `地址拿到了但**没人应答**：${base}（探的是 /healthz）。\n` +
        `地址来自 ${input.routePath} → ${input.portFile}；` +
        '通常意味着 Web 刚起还没监听、或它已经退出（端口文件是残留）。' +
        '本面板不挂一个注定空白的 iframe——稍后自动重试。',
    }
  }
  // 简报 = Web 根（AI 监控已不在本 iframe 里：它改走共享资产的**原生 tab**，不走两跳）
  return { kind: 'ready', src: base + '/' }
}

// ⚠ R17 自查（照资产 README「抄完照 R17 自查一遍」）删掉了原先的 `shouldRetry(render)`：
// 它对**任何** error 都恒返回 true ⇒ 调用点那句 `if (render.kind === 'error' && !shouldRetry(render))`
// **永远不成立**，是个"看起来是缝、其实是死的"分支。**当前所有错误态都值得重试**
// （地址未发布 / 右栏稍后才挂上 / Web 还在启动，都是瞬时态）⇒ 直接**无条件重试**，
// 不留假缝。将来真出现"不可重试的错"（如配置写错），再让它**带在状态上**
// （`{ kind:'error'; retryable: boolean }`）并配「换掉它、结果就变」的用例。
