import { defineConfig } from 'tsdown'

/**
 * 两个产物（对应 package.json 的 "." 与 "./client" 导出）：
 *  · lib/index.mjs  —— HOST 半（node 平台）：把 @modelcontextprotocol/sdk + ws **打进** bundle
 *    （自包含，运行时不依赖 dsh 解析我们的 node_modules）；只 @deepseek-ai/* peer 外置（宿主提供）。
 *  · lib/client.js  —— CLIENT 半（browser 平台）：react 外置（dsh web client 提供，避免双 React）。
 * dts 关（类型走 src + types/shim；--patch 源加载本就直读 src）。
 */
const dshExternal = [/^@deepseek-ai\//]

/**
 * ⭐ **`@mecha/dsh-panel` 必须**打进** bundle（`alwaysBundle`），不许外置**（2026-10-01 实测）：
 * 它是 `file:` 依赖（junction）且**发布物是 `.ts` 源**。若按默认"依赖即外置"处理，产物里会留下
 * `import … from "@mecha/dsh-panel/mcp-bridge.ts"` ⇒ 走**构建产物那条交付路径**（`cordis.patch.yml`
 * → `lib/index.mjs`）时，运行期要去 node_modules 里加载 `.ts`，而 **Node 原生剥类型明确拒绝
 * `node_modules` 下的 `.ts`**（`ERR_UNSUPPORTED_NODE_MODULES_TYPE_STRIPPING`）⇒ 运行时就炸。
 * 与本文件既有的"自包含、运行时不依赖 dsh 解析我们的 node_modules"同一条意图。
 * ⚠ 这是**消费者侧**的打包配置（mecha 未核这一条；EL 已独立实测同一结论并采用同样的
 * `deps.alwaysBundle`；本仓前缀与 EL 对齐为 `@mecha/`，将来 mecha 再发包自动覆盖）。
 */
const panelInline = [/^@mecha\//]

export default defineConfig([
  {
    entry: { index: 'src/index.ts' },
    outDir: 'lib',
    format: 'esm',
    platform: 'node',
    target: 'node20',
    deps: { neverBundle: dshExternal, alwaysBundle: panelInline },
    dts: false,
    sourcemap: true,
    clean: true,
  },
  {
    entry: { client: 'src/client/index.ts' },
    outDir: 'lib',
    // dsh 的 web client 把所有插件的 client.js 合成一个 **classic-script 合并包**，每个模块
    // 必须自注册到 `window.__ModuleLoader__.load({id, factory:(require)=>{...}})`。普通 ESM
    // （顶层 import/export）在合并包里是 SyntaxError → 整包崩。故这里精确复刻该格式：
    // cjs + banner/intro/footer 包裹。
    format: 'cjs',
    platform: 'browser',
    target: 'es2022',
    // react / react/jsx-runtime 是 dsh 的 PLATFORM_MODULES（模块表提供）→ 外置，经注入的
    // require 解析，共享 dsh 的 React 实例（避免双 React）。@deepseek-ai/* 在 client 侧全是
    // 类型导入（已擦除），一并外置以防万一。⚠ 资产包**外置见上**（client 半同样必须打进）。
    deps: {
      neverBundle: ['react', 'react/jsx-runtime', ...dshExternal],
      alwaysBundle: panelInline,
    },
    dts: false,
    sourcemap: true,
    clean: false,   // 别清掉上一步的 host 产物
    outputOptions: {
      entryFileNames: 'client.js',
      banner: 'window.__ModuleLoader__.load({ id: "paperpilot-dsh", factory: (require) => {',
      intro: 'var module = { exports: {} }; var exports = module.exports;',
      footer: 'return module.exports; } });',
    },
  },
])
