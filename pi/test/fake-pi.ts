/** Just enough of Pi's ExtensionAPI and ExtensionContext to drive the extension without a Pi process. */
import type { ExtensionAPI, ExtensionContext } from '@earendil-works/pi-coding-agent';

type Handler = (event: any, ctx: any) => unknown;

export interface Notice {
  text: string;
  level: string;
}

export interface FakePi {
  api: ExtensionAPI;
  flags: Record<string, boolean | string | undefined>;
  registeredFlags: string[];
  tools: Map<string, any>;
  commands: Map<string, any>;
  handlers: Map<string, Handler[]>;
  entries: Array<{ type: 'custom'; customType: string; data: unknown }>;
  activeTools: string[];
  /** Run every handler of an event in order and collect what they returned. */
  fire(event: string, payload: Record<string, unknown>, ctx: FakeCtx): Promise<unknown[]>;
}

export interface FakeCtx extends ExtensionContext {
  notices: Notice[];
  statuses: Array<string | undefined>;
  confirmAnswer: boolean;
  confirms: string[];
}

export function fakePi(flags: Record<string, boolean | string | undefined> = {}): FakePi {
  const fp: FakePi = {
    flags,
    registeredFlags: [],
    tools: new Map(),
    commands: new Map(),
    handlers: new Map(),
    entries: [],
    activeTools: ['read', 'bash', 'edit', 'write', 'grep', 'find', 'ls'],
    api: undefined as unknown as ExtensionAPI,
    async fire(event, payload, ctx) {
      const out: unknown[] = [];
      for (const h of this.handlers.get(event) ?? []) out.push(await h({ type: event, ...payload }, ctx));
      return out;
    },
  };
  fp.api = {
    registerFlag: (name: string) => {
      fp.registeredFlags.push(name);
    },
    getFlag: (name: string) => fp.flags[name],
    registerTool: (def: { name: string }) => {
      fp.tools.set(def.name, def);
    },
    registerCommand: (name: string, opts: unknown) => {
      fp.commands.set(name, opts);
    },
    on: (event: string, handler: Handler) => {
      fp.handlers.set(event, [...(fp.handlers.get(event) ?? []), handler]);
      return () => undefined;
    },
    appendEntry: (customType: string, data: unknown) => {
      fp.entries.push({ type: 'custom', customType, data });
    },
    getActiveTools: () => fp.activeTools,
    setActiveTools: (names: string[]) => {
      fp.activeTools = names;
    },
  } as unknown as ExtensionAPI;
  return fp;
}

export function fakeCtx(
  fp: FakePi,
  opts: { cwd: string; sessionId?: string; saved?: boolean; hasUI?: boolean } = { cwd: process.cwd() },
): FakeCtx {
  const notices: Notice[] = [];
  const statuses: Array<string | undefined> = [];
  const ctx = {
    notices,
    statuses,
    confirmAnswer: true,
    confirms: [] as string[],
    cwd: opts.cwd,
    hasUI: opts.hasUI ?? true,
    mode: 'tui',
    signal: undefined,
    isIdle: () => true,
    ui: {
      notify: (text: string, level = 'info') => {
        notices.push({ text, level });
      },
      setStatus: (_key: string, text: string | undefined) => {
        statuses.push(text);
      },
      theme: { fg: (_c: string, t: string) => t },
      confirm: async (title: string, body: string) => {
        ctx.confirms.push(`${title}: ${body}`);
        return ctx.confirmAnswer;
      },
      input: async () => 'dcp_000000000000000000000000000000000000000000',
    },
    sessionManager: {
      getSessionId: () => opts.sessionId ?? 'session-aaaaaaaa-1111',
      getSessionFile: () => (opts.saved === false ? undefined : '/tmp/fake-session.jsonl'),
      getEntries: () => fp.entries,
      getCwd: () => opts.cwd,
    },
  };
  return ctx as unknown as FakeCtx;
}
