/** The flags `pi --prized` takes, and what they mean once read. */

/** Seconds of quiet after a turn before the sandbox pauses itself. */
export const DEFAULT_PAUSE_SECS = 20;
/** Minutes without any request before Prized pauses a sandbox on its own (the net under a crashed Pi). */
export const DEFAULT_IDLE_MIN = 10;
/** A paused sandbox that nobody comes back to is deleted by Prized this long after it paused: a week for a saved session... */
export const AUTO_DELETE_MIN = 7 * 24 * 60;
/** ...a day for one that cannot be resumed (--no-session, print mode). */
export const AUTO_DELETE_UNSAVED_MIN = 24 * 60;

export interface Config {
  template?: string;
  resources?: { vcpu?: number; memoryGb?: number; diskGb?: number };
  pauseSecs: number;
  idleMin: number;
  maxRuntimeMin?: number;
  keep: boolean;
  pullOnExit: boolean;
}

type FlagValue = boolean | string | undefined;

/** Registered with `pi.registerFlag`; the README table is these. */
export const FLAGS: ReadonlyArray<{ name: string; type: 'boolean' | 'string'; description: string }> = [
  { name: 'prized', type: 'boolean', description: 'Run tools in a Prized sandbox' },
  { name: 'prized-template', type: 'string', description: 'Prized template (default: prized-base-v1)' },
  { name: 'prized-vcpu', type: 'string', description: 'vCPUs for a new Prized sandbox (1 to 32)' },
  { name: 'prized-memory', type: 'string', description: 'Memory in GB for a new Prized sandbox' },
  { name: 'prized-disk', type: 'string', description: 'Disk in GB for a new Prized sandbox' },
  {
    name: 'prized-pause',
    type: 'string',
    description: `Seconds after a turn before the sandbox pauses with its memory (default ${DEFAULT_PAUSE_SECS}, 0 = never)`,
  },
  {
    name: 'prized-idle',
    type: 'string',
    description: `Minutes before Prized pauses a sandbox nobody is using (default ${DEFAULT_IDLE_MIN}, 0 = never)`,
  },
  { name: 'prized-max-runtime', type: 'string', description: 'Minutes of running time before Prized deletes the sandbox' },
  { name: 'prized-keep', type: 'boolean', description: 'On exit, pause the sandbox for pi -c instead of deleting it' },
  { name: 'prized-pull', type: 'boolean', description: 'On exit, apply the sandbox changes to your checkout without asking' },
];

function num(
  flag: string,
  value: FlagValue,
  { min, max, integer }: { min: number; max?: number; integer: boolean },
): number | undefined {
  if (value === undefined || value === '') return undefined;
  const n = typeof value === 'string' ? Number(value) : Number.NaN;
  const range = max === undefined ? `${min} or more` : `${min} to ${max}`;
  if (!Number.isFinite(n) || n < min || (max !== undefined && n > max) || (integer && !Number.isInteger(n))) {
    throw new Error(`--${flag} must be ${integer ? 'a whole number' : 'a number'}, ${range}`);
  }
  return n;
}

/** Read the flags. Throws an Error whose message says which flag is wrong. */
export function readConfig(getFlag: (name: string) => FlagValue): Config {
  const text = (name: string): string | undefined => {
    const v = getFlag(name);
    return typeof v === 'string' && v !== '' ? v : undefined;
  };
  const vcpu = num('prized-vcpu', getFlag('prized-vcpu'), { min: 1, max: 32, integer: true });
  const memoryGb = num('prized-memory', getFlag('prized-memory'), { min: 0.5, max: 128, integer: false });
  const diskGb = num('prized-disk', getFlag('prized-disk'), { min: 1, max: 128, integer: true });
  const resources = vcpu === undefined && memoryGb === undefined && diskGb === undefined ? undefined : { vcpu, memoryGb, diskGb };
  return {
    template: text('prized-template'),
    resources,
    pauseSecs: num('prized-pause', getFlag('prized-pause'), { min: 0, max: 3600, integer: true }) ?? DEFAULT_PAUSE_SECS,
    idleMin: num('prized-idle', getFlag('prized-idle'), { min: 0, max: 40320, integer: true }) ?? DEFAULT_IDLE_MIN,
    maxRuntimeMin: num('prized-max-runtime', getFlag('prized-max-runtime'), { min: 1, max: 10080, integer: true }),
    keep: getFlag('prized-keep') === true,
    pullOnExit: getFlag('prized-pull') === true,
  };
}
