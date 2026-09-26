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
    on(event: string, listener: (payload: never) => void): () => void
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

declare module '@deepseek-ai/dsh-host-webserver' {
  export interface IndexInjection {
    kind: 'global' | string
    name: string
    value: unknown
  }
}

declare module '@deepseek-ai/dsh-client-ui-slots' {
  export type PropsRuntime<TName extends string> = { name: TName }
}
