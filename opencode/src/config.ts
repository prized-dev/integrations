/**
 * What the plugin takes: options from opencode.json, and the key and endpoints from the environment only.
 *
 * The API key and the API's address are read from the environment, never from a config file: a project's
 * opencode.json is checked in and travels with the repository, and an address in it could send your key to
 * someone else's server.
 */

/** Seconds of quiet after a turn before the sandbox pauses itself. */
export const DEFAULT_PAUSE_SECS = 20;
/** Minutes without any request before Prized pauses a sandbox on its own (the net under a crashed OpenCode). */
export const DEFAULT_IDLE_MIN = 10;
/** A paused sandbox nobody comes back to is deleted by Prized this long after it paused. */
export const AUTO_DELETE_MIN = 7 * 24 * 60;

export interface Config {
  template?: string;
  resources?: { vcpu?: number; memoryGb?: number; diskGb?: number };
  pauseSecs: number;
  idleMin: number;
  maxRuntimeMin?: number;
  /** On exit, keep a sandbox with nothing to pull (paused) instead of deleting it. */
  keep: boolean;
}

export interface Env {
  apiKey?: string;
  baseUrl?: string;
  edgeUrl?: string;
}

/** Options as OpenCode hands them over: whatever JSON the user wrote. */
export type Options = Record<string, unknown> | undefined;

function num(
  name: string,
  value: unknown,
  { min, max, integer }: { min: number; max: number; integer: boolean },
): number | undefined {
  if (value === undefined || value === null || value === '') return undefined;
  const n = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(n) || n < min || n > max || (integer && !Number.isInteger(n))) {
    throw new Error(`opencode-prized option "${name}" must be ${integer ? 'a whole number' : 'a number'} from ${min} to ${max}`);
  }
  return n;
}

const KNOWN = new Set(['template', 'vcpu', 'memoryGb', 'diskGb', 'pauseSeconds', 'idleMinutes', 'maxRuntimeMinutes', 'keep']);

/** Read the plugin options. Throws an Error that names the option that is wrong. */
export function readConfig(options: Options): Config {
  const o = options ?? {};
  for (const k of Object.keys(o)) {
    if (!KNOWN.has(k)) {
      const hint = k === 'apiKey' || k === 'baseUrl' || k === 'edgeUrl' ? ' (set it in the environment: PRIZED_API_KEY, PRIZED_BASE_URL, PRIZED_EDGE_URL)' : '';
      throw new Error(`opencode-prized has no option "${k}"${hint}`);
    }
  }
  if (o.template !== undefined && (typeof o.template !== 'string' || !/^[\w.-]{1,64}$/.test(o.template))) {
    throw new Error('opencode-prized option "template" must be a template id such as prized-base-v1');
  }
  if (o.keep !== undefined && typeof o.keep !== 'boolean') throw new Error('opencode-prized option "keep" must be true or false');
  const vcpu = num('vcpu', o.vcpu, { min: 1, max: 32, integer: true });
  const memoryGb = num('memoryGb', o.memoryGb, { min: 1, max: 128, integer: false });
  const diskGb = num('diskGb', o.diskGb, { min: 10, max: 128, integer: true });
  return {
    template: o.template as string | undefined,
    resources: vcpu === undefined && memoryGb === undefined && diskGb === undefined ? undefined : { vcpu, memoryGb, diskGb },
    pauseSecs: num('pauseSeconds', o.pauseSeconds, { min: 0, max: 3600, integer: true }) ?? DEFAULT_PAUSE_SECS,
    idleMin: num('idleMinutes', o.idleMinutes, { min: 0, max: 40320, integer: true }) ?? DEFAULT_IDLE_MIN,
    maxRuntimeMin: num('maxRuntimeMinutes', o.maxRuntimeMinutes, { min: 1, max: 10080, integer: true }),
    keep: o.keep === true,
  };
}

/** The key and, for a control plane other than prized.dev, its addresses. */
export function readEnv(env: NodeJS.ProcessEnv = process.env): Env {
  const get = (k: string) => env[k]?.trim() || undefined;
  return { apiKey: get('PRIZED_API_KEY'), baseUrl: get('PRIZED_BASE_URL'), edgeUrl: get('PRIZED_EDGE_URL') };
}
