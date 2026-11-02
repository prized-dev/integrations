/**
 * The parts of `@prized/sdk` the provider uses, as structural types: the real `Sandbox` class
 * satisfies them, and so does the in-memory fake the offline tests inject.
 */
import type {
  ClientOptions,
  CreateOptions,
  ExecOptions,
  ExecResult,
  ForkOptions,
  ListOptions,
  SandboxInfo,
  SandboxState,
  WaitableOptions,
  WaitOptions,
} from '@prized/sdk';

/** A background command (`exec(..., { background: true })`). */
export interface CommandHandleLike {
  readonly pid: number;
  wait(options?: WaitOptions): Promise<ExecResult>;
  kill(signal?: string): Promise<boolean>;
  cleanup(): Promise<void>;
}

export interface FilesLike {
  readBytes(path: string): Promise<Uint8Array>;
  write(path: string, data: string | Uint8Array): Promise<unknown>;
}

/** One sandbox, as `Sandbox.create`, `connect`, `list` and `fork` return it. */
export interface SandboxLike {
  readonly id: string;
  readonly state: SandboxState;
  readonly info: SandboxInfo;
  readonly createdAt: Date | null;
  readonly files: FilesLike;
  exec(command: string | readonly string[], options: ExecOptions & { background: true }): Promise<CommandHandleLike>;
  exec(command: string | readonly string[], options?: ExecOptions & { background?: false }): Promise<ExecResult>;
  refresh(): Promise<unknown>;
  pause(options?: WaitableOptions): Promise<void>;
  fork(options?: ForkOptions): Promise<SandboxLike[]>;
  delete(options?: WaitableOptions): Promise<void>;
}

/** `Sandbox`'s static side. */
export interface SandboxApi {
  create(options?: CreateOptions): Promise<SandboxLike>;
  connect(id: string, options?: ClientOptions): Promise<SandboxLike>;
  list(options?: ListOptions): Promise<SandboxLike[]>;
  deleteByLabels(labels: Record<string, string>, options?: ClientOptions): Promise<string[]>;
}
