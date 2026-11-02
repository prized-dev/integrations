# Prized for herdr

Run Claude Code or Codex on a [Prized sandbox](https://prized.dev/docs/sandboxes?via=herdr) or [box](https://prized.dev/docs/boxes?via=herdr) from the [herdr](https://herdr.dev) worktree you are in. The plugin copies the worktree in, starts the agent in a tmux session there, and opens it in a pane beside yours. Closing the pane pauses the sandbox: its memory and processes survive, so the agent is where you left it when you reconnect.

## Install

You need herdr 0.8 or later, Node.js 22 or later, Git, and the `prized` CLI (0.21.9 or later), signed in:

```bash
curl -fsSL https://prized.dev/install.sh | sh
prized login
herdr plugin install prized-dev/integrations/herdr
```

The agent pane opens its shell with your `prized login`: Prized opens a shell in a sandbox for a person's sign-in, never for an API key. Everything else (create, copy, pause, delete) uses `PRIZED_API_KEY` instead when it is set in the environment herdr starts from, so it must be a key from the workspace you are signed in to ([make one](https://prized.dev/dashboard/workspace/api-keys?via=herdr)). The plugin hands the key to the `prized` CLI through the environment only: it never prints, logs or copies it.

## Use

Run an action from a keybinding, or from a shell in the workspace:

```bash
herdr plugin action invoke start-sandbox --plugin prized.sandbox
```

| Action | What it does |
| --- | --- |
| `start-sandbox` | Copies this worktree into a new sandbox and opens the agent beside this pane. |
| `start-box` | The same on one of your boxes (it asks which, or set `box`). |
| `reconnect` | Reopens the agent for this pane or worktree. A paused sandbox resumes where it stopped. |
| `apply-changes` | Shows what the agent changed since the copy, then applies it here after `git apply --check` passes and you say yes. |
| `pause` | Pauses the sandbox now. |
| `delete` | After you type `DELETE`, deletes the sandbox (on a box: stops the agent's session, keeps the files). |
| `add-machine` | Adds a box to herdr's sidebar as an SSH machine (below). |

To bind one to a key, add this to herdr's config:

```toml
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "prized.sandbox.start-sandbox"
description = "agent in a Prized sandbox"
```

## Closing the pane pauses the sandbox

When an agent pane closes, the sandbox is paused, not deleted. A paused sandbox keeps its memory, processes and files and bills storage only. Reconnect and the agent is mid-task where it stopped; nothing restarts. Set `"onClose": "keep"` to leave sandboxes running when their pane closes. As a backstop, a sandbox the plugin creates pauses itself after 30 idle minutes (`autoPause`).

Boxes are never paused by the plugin: the agent keeps running there with the pane, herdr and your laptop closed, and the box pauses itself when idle per its own [auto-pause](https://prized.dev/docs/boxes?via=herdr#auto-pause).

## What is copied, and what comes back

The copy holds the worktree's tracked files and untracked ones `.gitignore` does not ignore, minus `.git`, submodules, and files that usually hold credentials (`.env` and `.env.*` except `.env.example`, private keys, `.npmrc`, `.netrc`, Terraform state, cloud credential folders). It lands in `/home/prized/work/<repo>` on a sandbox, `~/herdr/<repo>-<id>` on a box, recorded in a fresh Git repository as the base.

The agent's changes stay there until you run `apply-changes`, which takes everything changed since the base (committed or not, new files and binary files included), shows the summary and applies it only if it applies cleanly. The next `apply-changes` shows only what changed after that.

On a sandbox, the agent's key goes with it: `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` for Claude Code, `OPENAI_API_KEY` for Codex, when set where herdr runs. It travels over stdin into a file only the sandbox's user can read, never on a command line. Set `"forwardEnv": []` to send none and sign the agent in inside the pane instead. Boxes keep their own sign-ins (`prized agents handoff claude codex --gh`).

## A box as a herdr machine

`add-machine` lists your boxes, wakes the one you pick if it is paused, checks that plain `ssh <box>` reaches it, offers to link the box's Claude Code and Codex into `/usr/local/bin` (herdr's panes on a machine get the system `PATH`), then runs `herdr machine add <box>`, which asks before it installs herdr there. The box then sits in the sidebar with its own spaces and agents. More in [Use a Prized box with the herdr app](https://prized.dev/docs/herdr-app?via=herdr).

## Settings

`config.json` in the directory `herdr plugin config-dir prized.sandbox` prints. Every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `agent` | `"claude"` | `claude`, `codex`, or `shell` |
| `command` | none | Any command to run instead, such as `"claude --dangerously-skip-permissions"` |
| `template` | `"prized-agent-v1"` | Sandbox template (`prized sandbox templates`) |
| `vcpu`, `memoryGb` | `2`, `4` | Sandbox shape |
| `autoPause`, `maxRuntime` | `"30m"`, none | Passed to `prized sandbox create` (`off` turns auto-pause off) |
| `onClose` | `"pause"` | `pause` or `keep` |
| `box` | none | The box `start-box` uses without asking |
| `forwardEnv` | the agent's key | Variable names to pass to the sandbox's agent |

## Limits

- Sandboxes run in Oregon (`us-west-2`) only.
- Sandboxes have no root and no Docker: the agent runs as `prized`, without sudo. Use a box for Docker or system packages.
- A worktree has to pack to 100 MB or less.
- The agent pane carries a terminal and nothing else: no port forwarding from a sandbox.
- On the free tier, a sandbox is 2 vCPU and 4 GB at most and runs 60 minutes in all; paused time does not count ([Free tier](https://prized.dev/docs/sandboxes?via=herdr#free-tier)).
- Today's `prized-agent-v1` build has no tmux, so the first start unpacks one into the sandbox's home (about 10 seconds) and adds two lines to the top of its `~/.bashrc`, so `prized sandbox ssh` finds the session.

## Tests

```bash
scripts/test.sh herdr
```

The tests run offline against stand-ins for `prized`, `herdr` and `ssh` (`test/fakes`); the stand-in sandbox runs the plugin's remote scripts with the real bash, tar and git. A live run creates one sandbox in your workspace and deletes it at the end: `herdr plugin link .` in this folder, then run `start-sandbox` from a pane in any Git repository, close the pane, `reconnect`, and `delete`.
