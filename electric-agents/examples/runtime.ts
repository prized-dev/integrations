/**
 * An Electric Agents runtime whose entities can run their tools on Prized. Typechecked with the
 * package (`npm run typecheck`); run it inside your own agents app, where `registry` holds your
 * entity types.
 */
import { createRuntimeHandler, type EntityRegistry } from '@electric-ax/agents-runtime';
import { unrestrictedSandbox } from '@electric-ax/agents-runtime/sandbox';

import { prizedSandboxProfile } from '../src/index.js';

export function makeRuntime(registry: EntityRegistry): ReturnType<typeof createRuntimeHandler> {
  return createRuntimeHandler({
    baseUrl: 'http://localhost:4437',
    registry,
    sandboxProfiles: [
      {
        name: 'local',
        label: 'Local',
        factory: () => unrestrictedSandbox({ workingDirectory: process.cwd() }),
      },
      // Reads PRIZED_API_KEY. Spawn with sandbox: { profile: 'prized' }.
      prizedSandboxProfile({ resources: { vcpu: 2, memoryGb: 4 } }),
    ],
  });
}
