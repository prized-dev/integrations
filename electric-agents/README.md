# @prized/electric-agents

> **Not on npm yet.** The install line below works once `@prized/electric-agents` is published. The Prized TypeScript SDK it uses (`@prized/sdk`) is on npm.

A sandbox profile for [Electric Agents](https://electric-sql.com/docs/agents/usage/sandboxing) on [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=electric-agents): an entity's bash, file and fetch tools run in a Firecracker microVM instead of on your runner. A persistent sandbox pauses with its memory and disk between wakes, and the next wake, on any runner, resumes it by key.

## Install

```sh
npm install @prized/electric-agents @electric-ax/agents-runtime
export PRIZED_API_KEY=dcp_...   # https://prized.dev/dashboard/workspace/api-keys?via=electric-agents
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=electric-agents.

Needs Node.js 22.13 or later and `@electric-ax/agents-runtime` 0.6 or later.

## Use

Add the profile to your runtime:

```ts
import { createRuntimeHandler } from '@electric-ax/agents-runtime';
import { prizedSandboxProfile } from '@prized/electric-agents';

const runtime = createRuntimeHandler({
  baseUrl: 'http://localhost:4437',
  registry,
  sandboxProfiles: [prizedSandboxProfile({ resources: { vcpu: 2, memoryGb: 4 } })],
});
```

Then pick it when you spawn:

```ts
await client.spawnEntity({ type: 'worker', id: 'fixer', sandbox: { profile: 'prized' } });
```

Electric's `scope`, `key`, `persistent` and `inherit` work as with its other profiles. For one sandbox outside a profile, `prizedSandbox({ sandboxKey, persistent, owner })` returns an Electric `Sandbox`, and `createPrizedClient()` returns the bare `RemoteSandboxClient` for `remoteSandbox({ provider: 'e2b', client })`.

## Options

| Option | Default | Meaning |
|---|---|---|
| `template` | `prized-base-v1` | Ubuntu 24.04 with git, curl, Python 3 and Node.js 22. `prized-agent-v1` adds Claude Code and Codex. |
| `resources` | the template's | `{ vcpu, memoryGb, diskGb }`. |
| `envVars` | none | Variables for every command. |
| `labels` | none | Your labels. `created-by: electric-agents`, the key label and `electric-entity-type` are added. |
| `keepAliveMs` | 2 minutes | Idle time before the sandbox pauses once no wake holds it (whole minutes). |
| `pausedRetentionMin` | 7 days | How long a persistent sandbox stays paused before Prized deletes it; `0` keeps it. |
| `maxRuntimeMin` | your plan's | Minutes of running before Prized deletes it: 24 hours on paid plans, 60 on the free tier. |
| `workingDirectory` | `/home/prized/work` | Must be inside `/home/prized`. |
| `lazy` | `true` | Create the sandbox on the first tool call, so a wake that runs no tool costs nothing. |
| `name`, `label`, `description` | `prized`, `Prized` | How the profile shows in Electric's picker. |
| `apiKey`, `baseUrl` | `PRIZED_API_KEY`, `https://api.prized.dev` | Your key and the API origin. |

## Behaviour and limits

- **Identity.** A sandbox is labelled with a hash of Electric's `sandboxKey`. A later wake lists by that label, resumes the sandbox if it is paused and attaches. An attacher (`inherit`) never creates one. If two runners create for one key at once, both keep the older sandbox and the newer is deleted.
- **Lifecycle.** While a wake holds the sandbox, a heartbeat keeps it running. On dispose an ephemeral sandbox is deleted. A persistent one is left to pause after `keepAliveMs`, with its memory and processes, and the next command resumes it. A terminal entity's `reclaim` deletes it by label, even if that wake never used it. A runner that dies leaves a sandbox that pauses on its own, and an ephemeral one is deleted a minute later.
- **Network.** Prized sandboxes always have outbound internet. A `deny-all` or `allowlist` policy throws rather than being ignored. `fetch()` runs inside the sandbox, as with Electric's e2b profile.
- **Files.** Reads and writes inside `/home/prized` go through the files API, binary-safe up to 100 MiB. Reads elsewhere in the VM, listings, stat, mkdir and remove run as one shell command each. New files are mode 0600.
- **Commands** run as the user `prized` with `bash -c`, without root. Output is kept whole up to 100 MiB a stream. stdin is uploaded as a file and redirected in. A timeout returns `timedOut: true` with what the command printed. Electric's `AbortSignal` is not passed to remote sandboxes.
- Sandboxes run in one region (US West).

## Tests

```sh
scripts/test.sh electric-agents                   # from the repo root: typecheck, build, offline tests
PRIZED_LIVE=1 npm run test:live                   # in this folder, with PRIZED_API_KEY: one 1 vCPU sandbox, about 15 seconds
```

The offline tests run the client against a stand-in for `@prized/sdk` backed by a local directory and a real `bash`, and run the profile through the published `@electric-ax/agents-runtime` (`remoteSandbox`, `lazySandbox`, `fetch()` inside the sandbox). The live test drives one keyed, persistent sandbox through two wakes with a pause in between, then reclaims it and waits for the API to answer 404.

## License

[Apache-2.0](LICENSE)
