/**
 * Prized Sandboxes as a sandbox provider for the Mastra editor.
 *
 * @example
 * ```typescript
 * import { prizedSandboxProvider } from '@prized/mastra';
 *
 * const editor = new MastraEditor({
 *   sandboxes: { [prizedSandboxProvider.id]: prizedSandboxProvider },
 * });
 * ```
 */
import type { SandboxProvider } from '@mastra/core/editor';

import { PrizedSandbox } from './sandbox.js';

/**
 * What the editor stores for a Prized sandbox. There is no API key or URL here on purpose: the
 * sandbox uses the server's PRIZED_API_KEY, so stored config can neither hold a key nor send it
 * anywhere else.
 */
export interface PrizedProviderConfig {
  id?: string;
  template?: string;
  vcpu?: number;
  memoryGb?: number;
  env?: Record<string, string>;
  timeout?: number;
  workingDirectory?: string;
}

export const prizedSandboxProvider: SandboxProvider<PrizedProviderConfig> = {
  id: 'prized',
  name: 'Prized Sandboxes',
  description: 'A Firecracker microVM from Prized that pauses with its memory and resumes by id',
  configSchema: {
    type: 'object',
    properties: {
      id: { type: 'string', description: 'Logical id: the sandbox created under it is reused' },
      template: { type: 'string', description: 'Template for a new sandbox (default prized-base-v1)' },
      vcpu: { type: 'number', description: 'vCPUs for a new sandbox' },
      memoryGb: { type: 'number', description: 'Memory in GB for a new sandbox' },
      env: { type: 'object', description: 'Environment variables', additionalProperties: { type: 'string' } },
      timeout: { type: 'number', description: 'Default command timeout in ms' },
      workingDirectory: { type: 'string', description: 'Default working directory for commands' },
    },
  },
  // Only known fields reach the sandbox.
  createSandbox: ({ id, template, vcpu, memoryGb, env, timeout, workingDirectory }) =>
    new PrizedSandbox({
      ...(id !== undefined && { id }),
      ...(template !== undefined && { template }),
      ...((vcpu !== undefined || memoryGb !== undefined) && {
        resources: { ...(vcpu !== undefined && { vcpu }), ...(memoryGb !== undefined && { memoryGb }) },
      }),
      ...(env !== undefined && { env }),
      ...(timeout !== undefined && { timeout }),
      ...(workingDirectory !== undefined && { workingDirectory }),
    }),
};
