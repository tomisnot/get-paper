/**
 * 本地最小类型 shim：@deepseek-ai/* 是 dsh 宿主提供的 peer，out-of-tree 插件本地装不到。
 * 只声明本插件用到的面（够 typecheck 即可，不追求完整）。
 */
declare module '@deepseek-ai/cordis' {
  export interface Context {
    logger?: { info(m: string): void; warn(m: string): void; error(m: string): void }
    tools: {
      register(def: unknown): () => void
    }
    slots: {
      inject(name: string, provide: () => unknown): () => void
      register(desc: { name: string; id: string; order?: number }, component: unknown): () => void
    }
    layout?: {
      openRightbar?(reserve: boolean, fullscreen: boolean): void
      closeRightbar?(): void
      toggleSidebar?(): void
    }
    /**
     * **scoped effect**：等 `deps` 里的服务就绪再跑回调，**回调返回值即 disposer**。
     * 与顶层 `export const inject` 的区别是"缺服务只是这段不跑"，而不是整个插件不激活
     * （后者会把"面板没有 web 服务"升级成"AI 工具也没了"）。
     */
    inject?(deps: string[], callback: (scoped: Context) => void | (() => void)): unknown
    /** dsh-host-webserver 提供的服务（仅 web 模式在场；headless 缺省）。 */
    webServer?: {
      register(route: {
        kind: 'exact' | 'prefix'
        path: string
        // 参数用 `any`（shim 只求够用）：`unknown` 会与实现侧的 `IncomingMessage`/
        // `ServerResponse` 逆变不兼容，逼实现去写更弱的类型。
        handler: (req: any, res: any) => void | Promise<void>
      }): () => void
    }
    effect(
      fn: () => void | (() => void | Promise<void>) | Promise<void | (() => void | Promise<void>)>,
      label?: string,
    ): Promise<void>
  }
}

declare module '@deepseek-ai/dsh-tools' {
  export interface ToolDefinition {
    name: string
    description: string
    parameters: unknown
    output: unknown
    execute(args: unknown, exec: ToolExecution): unknown
  }
  export interface ToolExecution {
    signal?: AbortSignal
  }
}

declare module '@deepseek-ai/dsh-client-ui-slots' {
  export type PropsRuntime<TName extends string> = { name: TName }
}
