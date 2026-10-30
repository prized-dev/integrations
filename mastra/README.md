# @prized/mastra

> **Coming 2026-11-05.** `@prized/mastra` is not on npm yet. The Prized TypeScript SDK it uses (`@prized/sdk`) is.

A [Mastra](https://mastra.ai) workspace sandbox on [Prized](https://prized.dev/docs/sandboxes?via=mastra): an agent's `execute_command`, background processes and file tools (read, write, edit, list, grep) run in a Prized sandbox, a Firecracker microVM, instead of on your server. `stop()` pauses the sandbox with its memory and disk, and the same `id` resumes it later, from this process or another.

## Install

```sh
npm install @prized/mastra @mastra/core
export PRIZED_API_KEY=dcp_...   # https://prized.dev/dashboard/workspace/api-keys?via=mastra
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=mastra. It can also install the Prized skill with `npx skills add https://prized.dev -g -y` (it should ask you first), so it knows when and how to use Prized the next time you ask for a sandbox.

Needs Node.js 22.13 or later and `@mastra/core` 1.79 or later.

## Use

```ts
import { Agent } from '@mastra/core/agent';
import { Workspace } from '@mastra/core/workspace';
import { PrizedSandbox } from '@prized/mastra';

const sandbox = new PrizedSandbox({ resources: { vcpu: 2, memoryGb: 4 } });
const workspace = new Workspace({ sandbox, filesystem: sandbox.filesystem });

const agent = new Agent({
  id: 'fixer',
  name: 'Fixer',
  instructions: 'Read and edit the code with the file tools, and run the tests with execute_command.',
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

## Files

`sandbox.filesystem` is the sandbox's own disk as a Mastra workspace filesystem. Pass it as the workspace's `filesystem` (a `Workspace` doesn't pick it up by itself) and Mastra's file tools act on the files the sandbox's commands see.

- Absolute paths are paths in the sandbox; relative ones start in the working directory (`/home/prized/work`). Paths aren't confined to a root: the agent's commands can read anything anyway.
- A read is one download. A write is one upload to a staging file plus one command that puts it in place, whole or not at all (and one check before, when it carries `expectedMtime`). A replaced file keeps its permission bits, so a `0755` script stays executable; a new one gets `0666` less the sandbox's umask.
- Stat, list, mkdir, delete, copy and move are one command each, and copies and moves send no file bytes over the network. `walk()` returns a whole tree in one command, so `list_files` doesn't make a request per directory. A listing bigger than one command's 4 MiB of output throws (code `E2BIG`): list a narrower directory.
- One read or write moves at most 100 MiB. Past that, `FileTooLargeError` (code `EFBIG`) is thrown before anything is sent: read part of the file with a command (`head -c`, `tail`, `sed -n`).
- Paths outside `/home/prized`, or behind a symlink that leaves it, are read and written through a staging file under `~/.cache/prized-mastra`, deleted after.
- `overwrite: false` writes and copies claim the destination atomically, and so do moves within one filesystem. A write with `expectedMtime` (Mastra's write and edit tools set it after a read) throws `StaleFileError` if the file changed since it was read.
- Each operation starts or resumes the sandbox if needed, and is retried once on a new sandbox if this one was deleted (which has none of the old files).
- There is no native `grep` yet: Mastra's grep tool lists and reads the files itself, a request per directory and per file.

## Lifecycle

| Mastra | Prized |
|---|---|
| `start()`, or the first command | Finds the sandbox labelled with `id` (or the one `sandboxId` names) and resumes it if it is paused; otherwise creates one. |
| `stop()` | Kills the background processes this instance started, then pauses the sandbox. Files, memory and other processes are kept; a paused sandbox bills storage only. |
| `destroy()` | Deletes the sandbox and everything in it, and any other sandbox labelled with the same `id`. |
| idle | A sandbox pauses itself after `autoPauseMin` idle minutes (default 10). The next command resumes it. A paused sandbox is kept until you delete it, unless you set `autoDeleteMin`. |
| sandbox gone | If the sandbox was deleted, or its run time ended, the next command starts a new one under the same `id` (with none of the old files). |

## Options

| Option | Default | Meaning |
|---|---|---|
| `id` | generated | Logical id: the sandbox created under it is reused. |
| `sandboxId` | none | Attach to this sandbox (`sbx_…`). If it no longer exists, falls back to `id`. `sandbox.sandboxId` reads the one in use. |
| `template` | `prized-base-v1` | `prized-base-v1` (Ubuntu 24.04, git, curl, Python 3, Node.js 22) or `prized-agent-v1` (adds Claude Code and Codex). |
| `resources` | the template's | `{ vcpu, memoryGb, diskGb }` for a new sandbox. |
| `labels` | none | Up to 31 labels for a new sandbox (`mastra-id` is always added). |
| `autoPauseMin` | `10` | Idle minutes before a new sandbox pauses itself; `0` never. |
| `autoDeleteMin` | never | Minutes after pausing before Prized deletes a new sandbox; `0` never. |
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
- A background process's output is read only while something follows it: `wait()`, or output callbacks (such as `backgroundProcesses` `onStdout` or `onExit` in the workspace tool config). `processes.get()` and `list()` read it once, and an abort signal kills it without following it. Reads count as activity, so a followed process keeps the sandbox from pausing itself; one nobody follows does not.
- `writeFiles()` uploads through the file API: paths inside `/home/prized`, up to 100 MiB per file; a `mode` is applied with `chmod`. Without one, a file it replaces may not keep its mode (images before r4 write `0600`); `sandbox.filesystem.writeFile()` always keeps it.
- Not supported: mounts, port URLs (`networking`), snapshots and checkpoints, computer use.
- Sandboxes run in one region (US West).
- Two processes starting the same new `id` at the same moment can create two sandboxes; later starts use the newest.

## Tests

```sh
npm test                                          # offline: a fake in place of the Prized API
PRIZED_LIVE=1 PRIZED_API_KEY=dcp_... npm run test:live   # against the real API
PRIZED_API_KEY=dcp_... OPENAI_API_KEY=sk-... npm run example
```

Offline, the filesystem tests run its shell commands for real in a temp directory (they need `sh` with GNU coreutils and findutils). The live tests create a few 2 vCPU sandboxes labelled `run=<this run>` (add your own with `PRIZED_TEST_LABELS=k=v,k=v`) and delete them at the end, failures included. The example's sandbox is deleted too, and it stops once the model's tokens would cost more than `MAX_USD` (default 0.10).

## License

Apache-2.0
