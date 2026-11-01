# Claude Code self-hosted environments on Prized

Run Claude Code cloud sessions (from claude.ai/code, the Claude app, `claude --cloud` and cloud scheduled tasks) on Prized, through Anthropic's orchestrator and the `spawn-runner` hook in this folder. Each session gets a [Prized sandbox](https://prized.dev/docs/sandboxes?via=claude-code-self-hosted) of its own, or runs on the Prized box of the person who started it.

Self-hosted environments are a public beta on Claude Team and Enterprise, off by default: an Owner turns on **Allow self-hosted environments** at claude.ai/admin-settings/cloud-environments.

## Run it

The guide has every step, with this file inlined: [Run the Claude Code self-hosted runner on Prized](https://prized.dev/docs/claude-code-self-hosted-runner?via=claude-code-self-hosted). In short, on a Prized box that stays on:

1. Save the environment key in `~/.config/claude-runner/environment-secret` and a Prized API key in `~/.config/claude-runner/prized-api-key`, both mode 600. The key needs **Sandboxes: Write**, or for own-box mode **Boxes: Write** and **Workspace: Read** ([API keys](https://prized.dev/dashboard/workspace/api-keys?via=claude-code-self-hosted)).
2. Copy `spawn-runner` to `~/.config/claude-runner/hooks/`, make it executable, and run `spawn-runner serve` under systemd (the unit is in the guide). For own-box mode, add `Environment=PRIZED_RUN_ON=own-box` to the unit.

## Two modes

| | `sandbox` (default) | `own-box` |
| --- | --- | --- |
| Where a session runs | A fresh `prized-agent-v1` sandbox, deleted once its runner exits | The Prized box of the workspace member whose email started the session, woken first if it sleeps |
| Git | Anthropic-managed git, as the session's GitHub account | The box's own git sign-in; its `~/.gitconfig` stays as it is |
| What it has | Only what `~/.config/claude-runner/sandbox-home/` copies in | Everything on that box: files, tools, sign-ins, running services |
| Lifetime | Released after 30 idle minutes; the sandbox is deleted | Released after 30 idle minutes; the box goes back to its own pause settings |
| Credential on the machine | The session's single-use work order | The session's single-use work order |

## Own-box mode

- **Which box.** The hook reads the workspace's members and boxes (`GET /api/v1/team/status`) and picks the one live box owned by the member who signs in to Prized with the session's Claude email. Case is ignored for A to Z only, as on Prized's side, so an email with a non-ASCII letter never matches someone else's. For someone with several boxes, or another email at Prized, add a line to `~/.config/claude-runner/own-boxes`: the Claude email, then the box's name or id.
- **Waking.** A sleeping box gets `POST /api/v1/boxes/{id}/wake`. If it is up within about 20 seconds, the runner starts inside the hook. If not, the hook keeps a copy of the work order (mode 600, deleted after), exits 0, and a waiter started beside it starts the runner once the box is up, before the work order's own expiry (read from it, on Anthropic's clock). The waiter holds a lock on its order: a redelivery starts nothing while it lives, and if it was killed (say with the orchestrator's unit), the redelivery starts a new one. `serve` clears copies a killed waiter left. `serve` sets the orchestrator's `--expected-spawn-seconds` to `CLAUDE_SPAWN_SECONDS` (600) for that.
- **Starting the runner.** Through the box's edge: one command makes `~/.claude-runner/orders/<order>/` and writes `~/.claude-runner/run-session`; the work order is uploaded on its own, mode 600; then `run-session` starts detached with `keepAwake`, so the box stays up until the runner exits. It runs `claude self-hosted-runner --capacity 1` in the first free slot, each with its own clone under `~/claude-sessions/<n>` (`CLAUDE_BOX_SLOTS`, 2). A box whose Claude Code is older than 2.1.309 runs `claude update` first. A box just woken whose Claude Code doesn't tell its version within 10 seconds gets a retry, not a refusal.
- **Idempotent.** A redelivered order finds its `started` marker and starts nothing; a second waiter for the same order isn't started.
- **No email in logs.** The hook never prints the session's email, as Anthropic asks, and passes it to `jq` and `awk` through the environment, not their arguments; the waiter runs without it.

Refusals reach the session's **Activity** tab as the hook's last line on stderr:

| The hook says | Exit | Fix |
| --- | --- | --- |
| `no member of the Prized workspace signs in with this session's Claude email` | 2 | Invite them, or map the email in `own-boxes` |
| `the person who started this session has no box in the Prized workspace` | 2 | They start a box, then send the session a message |
| `the person who started this session has 2 boxes (…)` | 2 | Name one in `own-boxes` |
| `this session has no account email` | 2 | A Slack channel or service session: give those another environment |
| `Prized would not wake <box> (409): wake_rejected: …` | 2 | Prized's own reason, such as an empty balance |
| `<box>: agent_upgrade_required: …` | 2 | Update the box agent, so it can hold the box awake |
| `<box> has no Claude Code …` | 2 | Install it on the box |
| `<box> already runs 2 sessions` | 1 | None: Anthropic offers the session again |
| Prized or the box answering 429 or 5xx, or a box still waking | 1 | None |

## Settings

Set them as `Environment=` lines in the unit.

| Variable | Default | What it does |
| --- | --- | --- |
| `PRIZED_RUN_ON` | `sandbox` | `own-box` runs each session on its person's box |
| `CLAUDE_IDLE_MIN` | `30` | Idle minutes before a runner releases its session and exits |
| `CLAUDE_MAX_MIN` | `480` | The longest a session runs before it is released |
| `CLAUDE_BOX_SLOTS` | `2` | Own-box: sessions at once on one box |
| `CLAUDE_SPAWN_SECONDS` | `600` | Own-box: how long a waking box has to start its runner (120 to 3600) |
| `PRIZED_TEMPLATE`, `PRIZED_VCPU`, `PRIZED_MEMORY_GB` | `prized-agent-v1`, the template's | Sandbox: the template and shape |
| `CLAUDE_CODE_VERSION` | the box's | Sandbox: the Claude Code version installed in each sandbox |
| `PRIZED_CLAUDE_DIR` | `~/.config/claude-runner` | Where the keys, `own-boxes`, logs and work-order copies live |

## Test

```bash
scripts/test.sh claude-code-self-hosted
```

Runs `spawn-runner` against a fake Prized API (control plane, sandbox toolbox and box edge) with a stub Claude Code, offline, with a fake key.

What ran beyond that, on 2026-10-29 (sandbox mode) and 2026-11-01 (own-box mode): Anthropic's real orchestrator against a local stand-in for its spawn queue, running this hook; and the real Prized API with a real box, whose real runner (Claude Code 2.1.316) took the hook's flags and reached api.anthropic.com, where the fake work order got the expected 401. Not yet run: a session from a real Claude Team or Enterprise organization.
