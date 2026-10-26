# @prized/mastra

> **Coming 2026-11-05.** `@prized/mastra` and the Prized TypeScript SDK it uses (`@prized/sdk`) are not on npm yet.

A [Mastra](https://mastra.ai) workspace sandbox on [Prized](https://prized.dev/docs/sandboxes): an agent's `execute_command` and background processes run in a Prized sandbox, a Firecracker microVM, instead of on your server. `stop()` pauses the sandbox with its memory and disk, and the same `id` resumes it later, from this process or another.

## Install

```sh
npm install @prized/mastra @mastra/core
export PRIZED_API_KEY=dcp_...   # https://prized.dev/dashboard/workspace/api-keys
```

Needs Node.js 22.13 or later and `@mastra/core` 1.79 or later.

## Use

```ts
import { Agent } from '@mastra/core/agent';
import { Workspace } from '@mastra/core/workspace';
import { PrizedSandbox } from '@prized/mastra';

const workspace = new Workspace({ sandbox: new PrizedSandbox({ resources: { vcpu: 2 } }) });

const agent = new Agent({
  id: 'fixer',
  name: 'Fixer',
  instructions: 'Use execute_command to run the tests and fix the code.',
  model: 'openai/gpt-5-nano',
  workspace,
});

try {
  await agent.generate('The tests in /home/prized/work/app fail. Make them pass.');
} finally {
  await workspace.destroy(); // deletes the sandbox
}
```

`examples/fix-bug.ts` is the full run: it seeds a bug, the agent fixes it, and the script checks the tests itself.

### One sandbox per user or thread

Give each sandbox an `id`. `start()` reuses the sandbox created under that id (a paused one resumes), or creates one:

```ts
import { Workspace } from '@mastra/core/workspace';
import { PrizedSandbox } from '@prized/mastra';

const workspace = new Workspace({
  sandbox: async ({ requestContext }) => {
    const sandbox = new PrizedSandbox({ id: `thread-${requestContext.get('thread')}` });
    await sandbox.start();
    return sandbox;
  },
  sandboxCacheKey: ({ requestContext }) => requestContext.get('thread') as string,
});

// When the thread ends, from any process:
await new PrizedSandbox({ id: 'thread-42' }).destroy();
workspace.clearSandboxCache('42');
```

The id is stored as the sandbox's `mastra-id` label (ids over 256 characters as their SHA-256), so `prized sandbox ls` and the API find it too.

## Lifecycle

| Mastra | Prized |
|---|---|
| `start()`, or the first command | Finds the sandbox labelled with `id` (or the one `sandboxId` names) and resumes it if it is paused; otherwise creates one. |
| `stop()` | Kills the background processes this instance started, then pauses the sandbox. Files, memory and other processes are kept; a paused sandbox bills storage only. |
| `destroy()` | Deletes the sandbox and everything in it. |
| idle | A sandbox pauses itself after `autoPauseMin` idle minutes (default 10). The next command resumes it. |
| sandbox gone | If the sandbox was deleted, or its run time ended, the next command starts a new one under the same `id` (with none of the old files). |

## Options

| Option | Default | Meaning |
|---|---|---|
| `id` | generated | Logical id: the sandbox created under it is reused. |
| `sandboxId` | none | Attach to this sandbox (`sbx_…`). If it no longer exists, falls back to `id`. `sandbox.sandboxId` reads the one in use. |
| `template` | `prized-base-v1` | `prized-base-v1` (Ubuntu 24.04, git, curl, Python 3, Node.js 22) or `prized-agent-v1` (adds Claude Code and Codex). |
| `resources` | the template's | `{ vcpu, memoryGb, diskGb }` for a new sandbox. |
| `labels` | none | Labels for a new sandbox (`mastra-id` is always added). |
| `autoPauseMin` | `10` | Idle minutes before a new sandbox pauses itself; `0` never. |
| `maxRuntimeMin` | your plan's | Minutes of running before Prized deletes a new sandbox: 24 hours by default on paid plans (up to 7 days, and every resume restarts the clock), 60 minutes on the free tier. |
| `timeout` | `300000` | Milliseconds a foreground command may run; `0` for no limit. A command's own `timeout` wins. Background processes have no limit unless they set one. |
| `env` | none | Variables for every command (`setEnv()` changes them later). They are passed per command and never written into the sandbox. |
| `workingDirectory` | `/home/prized/work` | Where commands start. |
| `instructions` | built in | Replace or extend what the agent is told about the sandbox. |
| `apiKey` | `PRIZED_API_KEY` | Your Prized API key. Requests go to `api.prized.dev` and the sandbox's own `term.prized.dev` URL. |

`onStart`, `onStop` and `onDestroy` work as in every Mastra sandbox. `sandbox.sandbox` is the `@prized/sdk` sandbox, for the rest of its API (files, fork, ...). `clone()` makes an unstarted copy with the same settings. For the Mastra editor, register `prizedSandboxProvider`; its stored config takes no key (the server's `PRIZED_API_KEY` is used).

## Behaviour and limits

- Commands run in a shell as the user `prized` (home `/home/prized`, no sudo). Install Python packages into a virtualenv (`python3 -m venv ~/.venv`); the system pip refuses.
- Commands get no stdin: `sendStdin()` throws, so LSP servers and other stdin protocols don't work.
- A command past its timeout is killed and returns exit code 124 with `timedOut: true`; a killed one returns 137 with `killed: true`. An aborted command is killed.
- `writeFiles()` writes through the file API: paths inside `/home/prized`, up to 100 MiB per file; a `mode` is applied with `chmod`.
- Not supported: the workspace's file tools (`filesystem`), mounts, port URLs (`networking`), snapshots and checkpoints, computer use. The agent works through `execute_command`.
- Sandboxes run in one region (US West).
- Two processes starting the same new `id` at the same moment can create two sandboxes; later starts use the newest.

## Tests

```sh
npm test                                          # offline unit tests, a fake in place of the Prized API
PRIZED_LIVE=1 PRIZED_API_KEY=dcp_... npm run test:live   # against the real API
PRIZED_API_KEY=dcp_... OPENAI_API_KEY=sk-... npm run example
```

The live tests create a few 2 vCPU sandboxes labelled `run=<this run>` (add your own with `PRIZED_TEST_LABELS=k=v,k=v`) and delete them at the end, failures included. The example's sandbox is deleted too, and it stops once the model's tokens would cost more than `MAX_USD` (default 0.10).

## License

Apache-2.0
