/**
 * opencode-prized: OpenCode's file and shell tools run in a Prized sandbox that pauses between turns with
 * its memory. Add "opencode-prized" to opencode.json and export PRIZED_API_KEY. See README.md.
 *
 * One default export for both OpenCode lines:
 * - OpenCode 1 reads `{ id, server }` (packages/opencode/src/plugin/shared.ts L272-300 at v1.19.5);
 * - OpenCode 2 reads `{ id, setup }` (packages/core/src/plugin/module.ts L80-95 at dev 5ea5c5e), and its
 *   schema ignores the extra `server` key, as OpenCode 1 ignores `setup`.
 */
import { createV1Plugin } from './src/v1.ts';
import { createV2Setup } from './src/v2.ts';

export default { id: 'prized', server: createV1Plugin(), setup: createV2Setup() };
