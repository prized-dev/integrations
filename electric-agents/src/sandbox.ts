/**
 * Electric `Sandbox`es and a sandbox profile on Prized, built on Electric's own
 * `remoteSandbox()`: its write containment, in-VM `fetch()` and dispose rules apply unchanged.
 */
import { lazySandbox, remoteSandbox } from '@electric-ax/agents-runtime/sandbox';
import type { Sandbox, SandboxProfile } from '@electric-ax/agents-runtime/sandbox';

import {
  assertPrizedNetworkPolicy,
  assertPrizedWorkingDirectory,
  createPrizedClient,
  DEFAULT_WORKING_DIRECTORY,
  reclaimPrizedSandboxByKey,
  type PrizedSandboxOptions,
} from './client.js';

/** `Sandbox.name` of a Prized sandbox, in Electric's `remote:<provider>` form. */
export const PRIZED_SANDBOX_NAME = 'remote:prized';

/**
 * A `Sandbox` on a Prized microVM, for one wake. Pass the identity Electric resolved
 * (`sandboxKey`, `persistent`, `owner`) to reattach across wakes and hosts; without a key it
 * makes a fresh sandbox, deleted on `dispose()` unless `persistent`.
 */
export async function prizedSandbox(opts: PrizedSandboxOptions = {}): Promise<Sandbox> {
  const workingDirectory = opts.workingDirectory ?? DEFAULT_WORKING_DIRECTORY;
  // Checked before anything is created, so a bad option never leaves a sandbox behind.
  assertPrizedWorkingDirectory(workingDirectory);
  assertPrizedNetworkPolicy(opts.initialNetworkPolicy);
  const client = await createPrizedClient({ ...opts, workingDirectory });
  // `provider` only names a remote sandbox that brings its own client: the e2b SDK is never
  // loaded. The name is set right below.
  const sandbox = await remoteSandbox({
    provider: 'e2b',
    client,
    workingDirectory,
    persistent: opts.persistent === true,
    owner: opts.owner !== false,
  });
  return withName(sandbox, PRIZED_SANDBOX_NAME);
}

/** What `prizedSandboxProfile` takes: the sandbox options, plus how the profile is shown. */
export interface PrizedSandboxProfileOptions extends Omit<PrizedSandboxOptions, 'sandboxKey' | 'persistent' | 'owner'> {
  /** The profile's wire name, chosen at spawn. Default `prized`. */
  name?: string;
  /** Shown in Electric's sandbox picker. Default `Prized`. */
  label?: string;
  description?: string;
  /**
   * Create the sandbox on first use rather than at the start of every wake, so a wake that
   * runs no tool costs nothing. Default `true`.
   */
  lazy?: boolean;
}

const DEFAULT_DESCRIPTION =
  'Runs in a Prized microVM. Persistent sandboxes pause with their memory between wakes and are reachable from any runner.';

/**
 * A sandbox profile for `createRuntimeHandler({ sandboxProfiles: [...] })`. It is marked
 * `remote`, so entities on different runners can share one sandbox by key.
 */
export function prizedSandboxProfile(opts: PrizedSandboxProfileOptions = {}): SandboxProfile {
  const { name = 'prized', label = 'Prized', description = DEFAULT_DESCRIPTION, lazy = true, ...sandboxOptions } = opts;
  const workingDirectory = sandboxOptions.workingDirectory ?? DEFAULT_WORKING_DIRECTORY;
  // Fail at registration, not at the first wake.
  assertPrizedWorkingDirectory(workingDirectory);
  assertPrizedNetworkPolicy(sandboxOptions.initialNetworkPolicy);
  return {
    name,
    label,
    description,
    remote: true,
    factory: async ({ sandboxKey, persistent, owner, entityType }) => {
      const labels = { ...sandboxOptions.labels };
      if (entityType.length > 0 && entityType.length <= 256) labels['electric-entity-type'] = entityType;
      const make = (): Promise<Sandbox> => prizedSandbox({ ...sandboxOptions, labels, sandboxKey, persistent, owner });
      if (!lazy) return make();
      return lazySandbox({
        name: PRIZED_SANDBOX_NAME,
        workingDirectory,
        factory: make,
        // A terminal entity whose last wake never used the sandbox still wipes the one earlier
        // wakes kept. Owners only: an attacher never removes the owner's sandbox.
        reclaim: owner
          ? async () => {
              await reclaimPrizedSandboxByKey(sandboxKey, sandboxOptions);
            }
          : undefined,
      });
    },
  };
}

/** The same sandbox under another `name`; every other member is the original's, bound to it. */
function withName(sandbox: Sandbox, name: string): Sandbox {
  return new Proxy(sandbox, {
    get(target, prop) {
      if (prop === 'name') return name;
      const value: unknown = Reflect.get(target, prop, target);
      return typeof value === 'function' ? (value as (...args: unknown[]) => unknown).bind(target) : value;
    },
  });
}
