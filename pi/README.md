# @prized/pi

> **Coming 2026-11-05.** `@prized/pi` is not on npm yet. The Prized TypeScript SDK it uses (`@prized/sdk`) is.

A [Pi](https://pi.dev) extension that runs the coding agent's `bash`, `read`, `write` and `edit` tools in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=pi), a Firecracker microVM, while Pi, the model calls and your files stay on your machine. The sandbox **pauses between turns with its memory**: a dev server or watcher the agent started is still running at the next command, and again after `pi -c` when the sandbox was kept. `/fork` copies the sandbox, processes and all.

## Install

```sh
npm install -g @earendil-works/pi-coding-agent   # Pi, needs Node.js 22.19 or later
pi install npm:@prized/pi
export PRIZED_API_KEY=dcp_...                     # prized.dev/dashboard/workspace/api-keys?via=pi
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=pi.

## Use

```sh
cd my-project
pi --prized
```

1. **Start.** Pi opens without waiting for the sandbox. The sandbox starts with your first prompt, and your git working tree goes up to it: tracked files and new files, with your uncommitted changes. Files git ignores, such as `.env`, stay on your machine.
2. **Work.** `bash`, `read`, `write`, `edit` and your `!` commands run in the sandbox. The agent can start a server or a watcher with `&` and the command returns when the shell does.
3. **Pause.** When a turn ends the sandbox pauses after the quiet time `--prized-pause` sets, memory and running processes included, and the next tool call resumes it. A paused sandbox bills storage only.
4. **Pull.** `/prized pull` shows what changed and applies it to your checkout once you confirm. `/prized pull --branch` commits it on the local branch `pi/<session>` instead (it needs your tracked files committed, and leaves you on that branch). Push and open the pull request with your own git. Files come back only this way.
5. **Come back.** `pi -c` finds the same sandbox, paused or running. `/fork` and `/clone` give the new session its own copy of the sandbox as it is now, running processes included, so you can try two approaches side by side; the original is left paused.
6. **Leave.** A sandbox with nothing left to pull is deleted. One with changes is kept, paused, and Pi says how to get them (a week later Prized deletes it if nobody came back).

To give Pi its task on the command line, put `--prized` after it (`pi -p "fix the failing test" --prized`) or write `--prized=true`: Pi reads the word after an extension flag as that flag's value, so `pi -p --prized "task"` loses the task. Add `--prized-pull` to bring the changes home when Pi exits, for work you would merge without reading.

## Flags

| Flag | |
| --- | --- |
| `--prized` | Run tools in a Prized sandbox |
| `--prized-vcpu <n>`, `--prized-memory <gb>`, `--prized-disk <gb>` | Size of a new sandbox (up to 32 vCPU; the free tier allows 2 vCPU and 4 GB) |
| `--prized-template <id>` | `prized-base-v1` (Ubuntu 24.04, git, curl, Python 3, Node.js 22, the default) or `prized-agent-v1` (adds Claude Code and Codex) |
| `--prized-pause <seconds>` | Quiet time after a turn before the sandbox pauses (default 20, `0` never) |
| `--prized-idle <minutes>` | Prized's own auto-pause for a sandbox nobody is using, the net under a crashed Pi (default 10, `0` never) |
| `--prized-max-runtime <minutes>` | Running minutes before Prized deletes the sandbox, and what you had not pulled with it (24 hours by default on paid plans, up to 7 days; 60 minutes in all on the free tier). Pi warns when 10 minutes are left |
| `--prized-keep` | On exit, pause the sandbox for `pi -c` instead of deleting it |
| `--prized-pull` | On exit, apply the sandbox's changes to your checkout without asking (see below) |

| Command | |
| --- | --- |
| `/prized` | Sandbox id, state, size, working directory, changes not pulled yet |
| `/prized pull`, `/prized pull --branch` | Apply the changes to your checkout, or commit them on a local branch |
| `/prized pause` | Pause now (the next tool call resumes it) |
| `/prized delete` | Delete the sandbox; the next tool call starts a new one from your checkout |

## What to know

- **What runs where.** Only the four tools run in the sandbox. Pi's own files, settings, `AGENTS.md` and the model calls stay on your machine, your environment variables are not forwarded, and `PRIZED_API_KEY` never enters the sandbox. If the sandbox cannot start, the tool call fails; it is never run on your machine instead.
- **The machine.** Commands run as the user `prized` (no sudo) in `/home/prized/work/<project>`, mirroring the folder you started Pi in. `pip install` needs a virtualenv (`python3 -m venv ~/.venv`).
- **`grep`, `find`, `ls` and `powershell` are off,** because Pi's versions act on your machine; the agent searches with `bash`. Pi's skills are files on your machine, so they are left out of the prompt.
- **Review what you pull.** The changes are what the agent wrote after reading your code, so read them as you would a pull request from someone else. `/prized pull` lists symlinks, new or newly executable files and files your tools run on their own (`.husky/`, `.envrc`, `.pi/`, `.claude/`, ... at any depth) before you confirm, and `--prized-pull` leaves such a change in the sandbox instead of applying it. A file that was executable already and stays so is not listed when it is edited, so read the diff. A conflict with your own edits applies nothing.
- **What comes back.** Files git would track: what the project's `.gitignore` ignores (`node_modules`, build output, `.env`) and anything outside the project folder stays in the sandbox and goes when it is deleted. A git repository the agent creates inside the project is left out of a pull, and named.
- **Size.** The project goes up as one tarball of at most 100 MiB packed; put big files in `.gitignore`. Git history does not go up (the sandbox repository starts with one commit), and submodules and nested repositories are left out.
- **No URL for a server yet.** Ports of a sandbox are not reachable from outside, so the agent cannot hand you a preview link.
- **If Pi is killed** (Ctrl-C in print mode, `kill -9`), the sandbox pauses itself after `--prized-idle` minutes, and Prized deletes it when its run time ends.
- **Forking.** The parent sandbox is left paused when a session is forked, so you can go back to it; `prized sandbox ls --label created-by=pi-prized` lists what this extension made.
- **Tested** on Linux: the offline tests on Pi 1.0.0 and 1.4.0, and the live test, which runs the real Pi 1.4.0 against the real API. The `git` and `tar` calls are written for both GNU and BSD `tar`, so macOS should work, but it has not been run. Native Windows has not been tried; Pi on WSL works like Linux.

## Cost

A running sandbox bills by the second for its vCPU, memory and disk; a paused one bills storage only ([Billing](https://prized.dev/docs/billing?via=pi)). Because it pauses after each turn, it bills compute while the agent works, not while you read.

## Tests

```sh
npm test               # offline: a fake of the Prized sandbox, Pi's real tools and agent loop, a scripted model
PRIZED_API_KEY=dcp_... npm run test:live   # the real Pi against the real API
```

Offline, the tests run the git, tar and shell commands the extension sends for real in temp directories (they need `git`, `tar` and `bash`). The live test creates up to two 1 vCPU sandboxes in your workspace, labelled `created-by=pi-prized`, drives Pi in RPC mode through a fix, a pause, a pull, a fork and a resume with a scripted model (no model key), and deletes them at the end.

## License

Apache-2.0
