/**
 * paperpilot dsh 插件 —— **CLIENT 入口**（浏览器侧 Cordis 插件）。
 *
 * ## ⭐ 2026-10 阶段②：本文件 = **那一行 bootstrap，别的都不写**
 *
 * ```ts
 * export { apply, name, inject } from '@mecha/dsh-panel/panel/auto.ts'   // ← 框架的代码
 * ```
 *
 * 那一行接上**框架自己的 client 插件**：它读框架起 dsh 时按**声明**
 * （`Get Paper/mecha_site.py` 的 `Service.monitor`）生成的那份参数，
 * **按服务挂 N 个 tab**（三家各挂自己那一份），头部按钮**恒为 1 个**（语义 = 把监控面带到眼前）。
 *
 * ⚠ 为什么**不是**裸 `import`：cordis 只认**模块自己的** `name`/`inject`/`apply`
 * —— 裸 import 只执行模块体、`apply` 不会被调用；且资产 `sideEffects:false` 还会把它摇掉。
 *
 * ## ⭐ 2026-10-09：**本仓的自定义 UI 也删了** ⇒ 本文件与 EL 那份**逐字同形**
 *
 * 原先这里还挂着一个会话头部按钮「☀ 评审今日」（`ReviewSopButton.tsx`：把固定评审 SOP
 * 一键复制进剪贴板）。⚠ **用户裁决**：它**已经完全不起作用**，而且会**挤在**框架那个
 * 唯一的「◈ 监控」按钮旁边（遮挡）⇒ **整条删掉**，代码空间归净。
 * ⇒ 现在本仓 client 半只剩这一行（`ReviewSopButton.tsx` 已随之删除，判据钉着"不许回来"）。
 *
 * ## 面板参数的家在哪
 *
 * ⭐ **在声明**（`mecha_site.py` 的 `monitor=MonitorSpec(...)`）；参数与**监控地址路由**
 * 都归框架（框架的 host 插件按 `MECHA_PANEL_PORT_FILES` 现取端口）。
 *
 * ⚠ client 半经 tsdown 打成 CJS + `__ModuleLoader__.load` 包装；改本目录源码后须
 * `npm run bundle` 并重启 dsh 才生效。
 */
export { apply, name, inject } from '@mecha/dsh-panel/panel/auto.ts'
