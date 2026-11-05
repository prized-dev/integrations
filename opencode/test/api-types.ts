/**
 * Compile-time only (npm run typecheck): the plugin against the real types of OpenCode 1's plugin API,
 * @opencode-ai/plugin 1.19.5. Nothing here runs.
 */
import type { Hooks, PluginInput, PluginModule } from '@opencode-ai/plugin';

import plugin from '../index.ts';
import { createV1Plugin } from '../src/v1.ts';

/** OpenCode's PluginInput gives the plugin what it reads, and the plugin returns OpenCode's Hooks. */
export async function v1(input: PluginInput): Promise<Hooks> {
  return createV1Plugin()(input, {});
}

/** The default export is a module OpenCode 1 loads. */
export const mod: PluginModule = plugin;
