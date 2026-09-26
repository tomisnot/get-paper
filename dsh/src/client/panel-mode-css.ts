/**
 * 面板模式 CSS：仅在 `html[data-pp-panel="on"]` 下生效（mode-store 打标）。
 *
 * 效果：隐藏左栏；中栏（AI 对话）fixed 到右上约 1/4；右栏铺满剩余高度放 PaperPilot iframe。
 * 列定位靠 mode-store 运行时打的 data-pp-* 标记（CSS Modules hash 类名选不中）。
 */
export const PANEL_MODE_CSS = `
html[data-pp-panel="on"] [data-pp-sidebar] { display: none !important; }
html[data-pp-panel="on"] [data-pp-center] {
  position: fixed !important;
  top: 0; right: 0;
  width: 44vw; height: 50vh;
  z-index: 40;
  border-left: 1px solid var(--border, #e5e7eb);
  border-bottom: 1px solid var(--border, #e5e7eb);
  background: var(--background, #fff);
}
html[data-pp-panel="on"] [data-rightbar-col] {
  position: fixed !important;
  top: 0; left: 0; bottom: 0;
  width: 56vw;
  z-index: 30;
  background: var(--background, #fff);
}
html[data-pp-panel="on"] .pp-panel-frame {
  width: 100%; height: 100%; border: 0; display: block;
}
/* 错误态：**绝不留白**——地址不详/容器缺失时贴一张可读说明卡（panel-state.ts 的文案）。 */
html[data-pp-panel="on"] .pp-panel-error {
  box-sizing: border-box;
  margin: 12px; padding: 12px 14px;
  border: 1px solid var(--border, #e5e7eb); border-left: 3px solid #cf222e;
  border-radius: 8px; background: #fff8f8; color: #1f2328;
  font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
}
/* 连右栏容器都没有时的兜底：贴页面左上角，别让人以为"点了没反应"。 */
html[data-pp-panel="on"] .pp-panel-error-overlay {
  position: fixed; top: 12px; left: 12px; max-width: 46vw; z-index: 90;
  box-shadow: 0 8px 24px rgba(0, 0, 0, .18);
}
`
