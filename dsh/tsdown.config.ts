import { defineConfig } from 'tsdown'

/**
 * 两个产物（对应 package.json 的 "." 与 "./client" 导出）：
 *  · lib/index.mjs  —— HOST 半（node 平台）：把 @modelcontextprotocol/sdk + ws **打进** bundle
 *    （自包含，运行时不依赖 dsh 解析我们的 node_modules）；只 @deepseek-ai/* peer 外置（宿主提供）。
 *  · lib/client.js  —— CLIENT 半（browser 平台）：react 外置（dsh web client 提供，避免双 React）。
 * dts 关（类型走 src + types/shim；--patch 源加载本就直读 src）。
 */
const dshExternal = [/^@deepseek-ai\//]

export default defineConfig([
  {
    entry: { index: 'src/index.ts' },
    outDir: 'lib',
    format: 'esm',
    platform: 'node',
    target: 'node20',
    external: dshExternal,
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
    // 类型导入（已擦除），一并外置以防万一。
    external: ['react', 'react/jsx-runtime', ...dshExternal],
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
