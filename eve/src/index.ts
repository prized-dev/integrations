/**
 * Prized Sandboxes for eve.
 *
 * ```ts
 * // agent/sandbox.ts
 * import { defineSandbox } from 'eve/sandbox';
 * import { PrizedSandbox } from '@prized/eve-sandbox';
 *
 * export const environment = PrizedSandbox.environment({ resources: { vcpu: 2, memoryGb: 4 } });
 * export default defineSandbox(() => environment.open());
 * ```
 *
 * @see https://prized.dev/docs/sandboxes?via=eve
 */
import { Sandbox } from '@prized/sdk';
import type { SandboxSession } from 'eve/sandbox';
import {
  SANDBOX_AGENT_ENV,
  SandboxSessionGoneError,
  SandboxTemplateNotProvisionedError,
  defineSandboxProvider,
} from 'eve/sandbox/provider';
import type { SandboxProvider, WithPrepareOption } from 'eve/sandbox/provider';

import type { PrizedSandboxExtensions } from './connection.ts';
import { PROVIDER_NAME, createPrizedImplementation } from './provider.ts';
import type { PrizedSandboxOpenOptions, PrizedSandboxOptions } from './provider.ts';

/** The sandbox a Prized environment opens: eve's session plus `sandboxId`. */
export type PrizedSandboxSession = SandboxSession & PrizedSandboxExtensions;

/** `PrizedSandbox.environment()`'s options plus eve's standard `prepare`. */
export type PrizedSandboxEnvironmentOptions = WithPrepareOption<PrizedSandboxOptions, PrizedSandboxSession>;

/** The Prized Sandboxes provider: `PrizedSandbox.environment(options)` in `agent/sandbox.ts`. */
export const PrizedSandbox: SandboxProvider<
  PrizedSandboxEnvironmentOptions,
  PrizedSandboxOpenOptions,
  PrizedSandboxSession
> = defineSandboxProvider({
  name: PROVIDER_NAME,
  stateProtocolVersion: 1,
  environment: (options: PrizedSandboxOptions) =>
    createPrizedImplementation(options, Sandbox, {
      SANDBOX_AGENT_ENV,
      SandboxSessionGoneError,
      SandboxTemplateNotProvisionedError,
    }),
});

export {
  BUILD_LABEL,
  DEFAULT_AUTO_PAUSE_MIN,
  SESSION_LABEL,
  TEMPLATE_LABEL,
  sessionLabel,
} from './provider.ts';
export type {
  PrizedSandboxOpenOptions,
  PrizedSandboxOptions,
  PrizedSessionState,
  PrizedTemplateArtifact,
} from './provider.ts';
export { DEFAULT_WORKSPACE_ROOT } from './connection.ts';
export type { PrizedSandboxExtensions } from './connection.ts';
