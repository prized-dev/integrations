# opencode-prized

> **Not on npm yet.** Until it is, install it from this repo (below). The Prized TypeScript SDK it uses (`@prized/sdk`) is on npm.

An [OpenCode](https://opencode.ai) plugin that runs the agent's `bash`, `read`, `write`, `edit`, `glob`, `grep` and `apply_patch` tools in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=opencode), a Firecracker microVM. OpenCode, the model calls and your files stay on your machine. The sandbox **pauses between turns with its memory**, so a dev server or watcher the agent started is still running next turn. A forked session gets its own copy of the sandbox, running processes included.

## Install

1. Get a key at [prized.dev](https://prized.dev/dashboard/workspace/api-keys?via=opencode) and export it where you start OpenCode:

   ```sh
   export PRIZED_API_KEY=dcp_...
   ```

   No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=opencode.

2. Get the plugin (until it is on npm):

   ```sh
   git clone https://github.com/prized-dev/integrations && cd integrations/opencode && npm install --omit=dev
   ```

3. Add it to `opencode.json` in your project (or `~/.config/opencode/opencode.json` for every project):

   OpenCode 1 (`opencode-ai`):

   ```json
   { "plugin": ["file:///path/to/integrations/opencode"] }
   ```

   OpenCode 2 (`@opencode/cli`):

   ```json
   { "plugins": ["/path/to/integrations/opencode"] }
   ```

   Once it is on npm, the entry is just `"opencode-prized"`.

## Use

1. **Start.** Run `opencode` as usual. The first tool call starts the sandbox and uploads your git working tree: tracked files and new files, with your uncommitted changes. Files git ignores, such as `.env`, stay on your machine.
2. **Work.** The tools run in the sandbox, in `/home/prized/work/<project>`. Paths the model gives under your project folder map there.
3. **Pause.** When the session goes idle, the sandbox pauses after `pauseSeconds` (20 by default), memory included; the next tool call resumes it. A paused sandbox bills storage only.
4. **Pull.** `/prized-pull` brings the changes to your checkout, unstaged, for `git diff`. If they include a symlink, an executable (new, made executable, or edited) or a file your tools run or load on their own (`.husky/`, `.envrc`, `.opencode/`, `.github/workflows/`, `AGENTS.md`, `.gitignore`, ...), it applies nothing, lists them with their diff, and waits for `/prized-pull --all`, which applies exactly the changes that review showed. `--branch` commits on the local branch `opencode/<session>` instead, with your git hooks off. Files come back only this way.
5. **Fork.** Fork a session in OpenCode, and the fork's first tool call copies the sandbox as it is now. The parent's sandbox is left as it was.
6. **Leave.** When OpenCode exits, a sandbox with nothing left to pull is deleted (set `keep` to keep it, processes and all). One with changes is paused and kept, and OpenCode prints how to get them; continuing the session reconnects to it, with its processes. Prized deletes a paused sandbox a week after it paused.

`/prized` shows the sandbox: id, state, size and what has not been pulled.

## Options

In `opencode.json`, as `["file:///path/to/integrations/opencode", { ... }]` (OpenCode 1) or `{ "package": "...", "options": { ... } }` (OpenCode 2):

| Option | |
| --- | --- |
| `template` | `prized-base-v1` (Ubuntu 24.04, git, curl, ripgrep, Python 3, Node.js 22; the default) or `prized-agent-v1` |
| `vcpu`, `memoryGb`, `diskGb` | Size of a new sandbox (up to 32 vCPU; the free tier allows 2 vCPU and 4 GB) |
| `pauseSeconds` | Quiet time after a turn before the sandbox pauses (default 20, `0` never) |
| `idleMinutes` | Prized's own pause for a sandbox nobody is using, the net under a crashed OpenCode (default 10) |
| `maxRuntimeMinutes` | Running minutes before Prized deletes the sandbox and what you had not pulled (24 hours on paid plans) |
| `keep` | On exit, keep a sandbox with nothing to pull (paused) instead of deleting it |

The key comes only from `PRIZED_API_KEY` in the environment, never from a config file. `PRIZED_BASE_URL` and `PRIZED_EDGE_URL` point the plugin at a control plane other than prized.dev.

## OpenCode itself in a sandbox

Normally only the tools run in a sandbox, and no key goes into it. If you run OpenCode itself in a sandbox (a CI runner, a cloud agent), keep your `PRIZED_API_KEY` out of it: on the machine that holds the key, mint a short-lived scoped key and pass only that in.

```ts
import { Sandbox } from '@prized/sdk';
import { mintSandboxKey } from 'opencode-prized/scoped'; // TypeScript: run with Bun or tsx

const key = await mintSandboxKey({ spendCapUsd: 5 }); // from your PRIZED_API_KEY
if (!key.available) throw new Error(key.reason);       // nothing was minted
const runner = await Sandbox.create({ envVars: key.env });
// ... install OpenCode and this plugin in `runner` and run it there
await key.revoke();                                    // when the run is done
```

- The key lasts 4 hours (`ttlSeconds`, up to a day, never past your own key). It reaches only the sandboxes the plugin makes with it (and their forks), plus any carrying all of `labels`, and only for what the plugin does with them. It can't reach your boxes, secrets, workspace settings, billing or other keys, or mint another key.
- A sandbox kept at exit is found again by the same key. A new key reaches it only through `labels`.
- If the installed `@prized/sdk` has no `Sandbox.scopedKey`, or the Prized API doesn't offer scoped keys (it answers 404), you get `{ available: false, reason }` and nothing is minted. Any other refusal throws. It never hands back your own key.
- Printing the result shows its id and expiry, not the key. `key.env` holds the key: pass it on, don't log it.

## What to know

- **What runs where.** Only the tools above run in the sandbox. Your environment variables are not forwarded, and if the sandbox cannot start, or an option is wrong, the tool fails: it never runs on your machine instead. A file that holds your `PRIZED_API_KEY` stops the upload. Tools that would act on your machine are off (`lsp`; in OpenCode 2 also image generation and worktree tools). Your own `!` shell commands, MCP servers and `webfetch` still run on your machine. If the plugin cannot load at all (its folder or dependencies missing), OpenCode runs its own tools as usual, so check that `/prized` answers.
- **The machine.** Commands run as the user `prized` (no sudo). `pip install` needs a virtualenv (`python3 -m venv ~/.venv`).
- **Undo.** OpenCode's `/undo` rewinds the conversation and your local files, not the sandbox: undo there with git or by asking the agent.
- **Size.** The project goes up as one tarball of at most 100 MiB packed. Git history does not go up, and submodules and nested repositories are left out.
- **No URL for a server yet.** A sandbox's ports are not reachable from your browser, so the agent cannot hand you a preview link.
- **Tested** live on Linux with OpenCode 1.19.5 and OpenCode 2.2.5 (see `DESIGN.md`). macOS should work the same; Windows is untested.

## Cost

A running sandbox bills by the second for its vCPU, memory and disk; a paused one bills storage only ([Billing](https://prized.dev/docs/billing?via=opencode)). It pauses after each turn, so it bills compute while the agent works, not while you read.

## Tests

```sh
npm install --legacy-peer-deps && npm run typecheck && npm test
```

Offline: a fake Prized sandbox that runs the plugin's real git, tar and bash commands in temp folders, OpenCode 1's plugin types, and stand-ins for both plugin APIs. `test/live/` drives the real OpenCode with a scripted model through a full session (see `DESIGN.md`).

## License

Apache-2.0
