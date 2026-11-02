# @prized/sandcastle

> **Coming 2026-11-05.** `@prized/sandcastle` is not on npm yet. The Prized TypeScript SDK it uses (`@prized/sdk`) is.

A [Sandcastle](https://github.com/mattpocock/sandcastle) sandbox provider for [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=sandcastle): each `run()` gets its own Firecracker microVM with Claude Code and Codex installed. Sandcastle syncs your repo in, the agent works and commits there, the commits come back to your branch, and the sandbox is deleted.

## Install

```sh
npm install @ai-hero/sandcastle @prized/sandcastle
export PRIZED_API_KEY=dcp_...   # https://prized.dev/dashboard/workspace/api-keys?via=sandcastle
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=sandcastle.

Needs Node.js 22.13 or later and `@ai-hero/sandcastle` 0.12 or later.

## Use

```ts
import { run, claudeCode } from '@ai-hero/sandcastle';
import { prized } from '@prized/sandcastle';

const result = await run({
  agent: claudeCode('claude-opus-4-8'),
  sandbox: prized(),
  prompt: 'Fix issue #42 in this repo.',
});
console.log(result.commits);
```

`createSandbox()` takes it the same way, for several runs (and `sandbox.exec()`) in one sandbox:

```ts
import { createSandbox, claudeCode } from '@ai-hero/sandcastle';
import { prized } from '@prized/sandcastle';

await using sandbox = await createSandbox({ branch: 'agent/fix-42', sandbox: prized({ resources: { vcpu: 4, memoryGb: 8 } }) });
await sandbox.run({ agent: claudeCode('claude-opus-4-8'), prompt: 'Fix issue #42.' });
const tests = await sandbox.exec('npm test');
```

The agent's own key goes in `.sandcastle/.env`, as with every Sandcastle sandbox: declare `CLAUDE_CODE_OAUTH_TOKEN=` or `ANTHROPIC_API_KEY=` for Claude Code (`CODEX_API_KEY=` for Codex) there, and Sandcastle passes it into the sandbox. Keep `PRIZED_API_KEY` out of that file: the provider reads it on your machine, and it never needs to reach the sandbox.

## Options

| Option | Default | Meaning |
|---|---|---|
| `template` | `prized-agent-v1` | Ubuntu 24.04 with git, Node.js, Python, Claude Code and Codex. `prized-base-v1` has no agent CLIs. |
| `resources` | the template's | `{ vcpu, memoryGb, diskGb }`. |
| `labels` | none | Labels for the sandbox (`created-by: sandcastle` is always added), shown in `prized sandbox ls`. |
| `autoPauseMin` | `10` | Idle minutes before the sandbox pauses itself; `0` never. A running command counts as activity, and the next one resumes a paused sandbox. |
| `autoDeleteMin` | `60` | Minutes after pausing before Prized deletes the sandbox; `0` never. With `autoPauseMin`, this cleans up a sandbox whose run was killed before it could delete it. |
| `maxRuntimeMin` | your plan's | Minutes of running before Prized deletes the sandbox: 60 on the free tier, 24 hours by default on paid plans. |
| `commandTimeoutMs` | `0` | Kill a command after this many milliseconds; `0` for no limit (Sandcastle's idle timeout still watches the agent). |
| `env` | none | Variables for every command, merged with Sandcastle's and the agent's. |
| `maxOutputTailChars` | `65536` | Characters of each stream a streamed command keeps for its result. Every line still reaches the display. |
| `apiKey` | `PRIZED_API_KEY` | Your Prized API key. |

## Behaviour and limits

- The repo lives at `/home/prized/workspace`. Commands run as the user `prized`, without root: `exec` and hooks with `sudo: true` throw. Install tools under `/home/prized` in an `onSandboxReady` hook (a Python virtualenv, a release binary, npm with a prefix in the home), or [set up one sandbox and fork it](https://prized.dev/docs/sandboxes-own-template?via=sandcastle).
- Output streams to Sandcastle line by line while the agent runs. A command's stdin (Sandcastle sends the prompt that way) goes up as a file and is redirected in, then removed.
- `copyIn` sends a directory as one tarball (it needs `tar` on your machine, which macOS, Linux and Windows 10+ have), so modes, links and empty directories come through. Files outside `/home/prized` (Sandcastle's bundle and patches in `/tmp`) pass through `~/.sandcastle-transfer` in the sandbox, emptied as they go.
- `close()` deletes the sandbox. Not supported: `interactive()`, Docker inside the sandbox.
- Sandboxes run in one region (US West).

## Tests

```sh
scripts/test.sh sandcastle                              # from the repo root: typecheck, build, offline tests
PRIZED_LIVE=1 npm run test:live                         # in this folder, with PRIZED_API_KEY: one 2 vCPU sandbox, about 15 seconds
```

The offline tests run the provider against a stand-in for `@prized/sdk` backed by a local directory, including a full Sandcastle `run()` (sync-in, a scripted agent's streamed exec, sync-out). The live test runs the handle contract against the real API and checks the sandbox is gone after `close()`.

Sandcastle's own verify harness (`npm run verify` in its repo: a real agent on its cheapest model, in a real sandbox, in an isolated world) passed with this provider on 2026-11-02, against Sandcastle `85e8e6d`:

- `claudeCode('claude-haiku-5-5')` × `prized()` through `run()`: one commit on `main`, no `sandcastle/*` branch left, ledger clean, sandbox deleted.
- The same through `createSandbox()` with a named branch: one commit on the branch, `HEAD` unmoved, ledger clean, sandbox deleted. The model shown in the agent's own session log was `claude-haiku-5-5`.

## License

[Apache-2.0](LICENSE)
