# Prized for Hermes Agent

Prized terminal backend for [Hermes Agent](https://github.com/NousResearch/hermes-agent): Hermes runs its terminal, file and `execute_code` commands in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=hermes), a Linux microVM that **pauses with its memory** when Hermes releases it (after `terminal.lifetime_seconds` idle, 300 s by default, or when Hermes exits) and resumes at the next command with its processes as they were. A dev server the agent started is still running, with the same PID, in this session or a later one (see [Background processes](#background-processes)). With `terminal.container_persistent: false` the sandbox is deleted instead when Hermes releases it.

It registers `terminal.backend: prized` through the pluggable terminal-backend extension point (hermes-agent PR #94400), so it appears in `hermes setup`, `hermes status` / `hermes doctor`, the dashboard picker and subprocess secret stripping with no change to Hermes core. It needs no SDK: it calls the [Prized HTTP API](https://prized.dev/docs/sandboxes-api?via=hermes) with httpx, which Hermes already ships.

## Install

```bash
hermes plugins install prized-dev/integrations/hermes --enable
hermes config set terminal.backend prized
```

Create an API key on the [Prized API keys page](https://prized.dev/dashboard/workspace/api-keys?via=hermes) (Full access, or Custom with **Sandboxes: Write**) and save it in `~/.hermes/.env` as `PRIZED_API_KEY=dcp_...`. No Prized account yet? Sign up at https://prized.dev/?via=hermes. `hermes doctor` shows a "Prized API key" row and the settings in effect.

## Behaviour

| | |
| --- | --- |
| Machine | Ubuntu 24.04 microVM from `prized-base-v1` (git, Python 3, Node.js 22, build tools). User `prized` without sudo, commands start in `/home/prized/work` |
| Pause | When Hermes releases the environment (its idle reaper, or exit), the sandbox is **paused**: memory and disk kept, compute not billed. The next command resumes it |
| Sharing | `scope: instance` (default): one sandbox per Hermes home, shared by every session, cron job and subagent, so it is one machine for your agent. `scope: session`: one sandbox per Hermes session key (gateway and WebUI sessions); CLI and cron runs share the default one. Known issue: two Hermes processes on one home (the CLI while the gateway runs) each count only their own users of the instance sandbox, so one releasing it pauses it under the other's running command until its next command resumes it; use one home per process (`scope: session` avoids it only between the CLI and gateway or WebUI sessions: the CLI and the gateway's cron jobs still share the default sandbox) |
| Lifetime | A sandbox is deleted when its run time is used up: 24 hours of running by default, up to 7 days with `max_runtime_min` (each resume restarts the clock), 60 minutes in all on the free tier. Paused time doesn't count: a paused sandbox is kept, for the price of its storage, until you delete it (on the free tier, until 30 days after its last use). In persistent mode (the default) the plugin never deletes a sandbox, and with `scope: session` each session leaves its own; `prized sandbox ls --label hermes=1` lists them. Once a sandbox is deleted, the next command starts a fresh one and its result carries `environment_recreated`, so the model knows processes and files there are gone |
| Safety net | By default every sandbox auto-pauses after 10 idle minutes (`auto_pause_min`), so a crashed Hermes never leaves one running |
| Files | Credential files a skill or `terminal.credential_files` names (never `.env` or `auth.json`), your skills (including external and project skill folders) and the `~/.hermes` cache are synced in (one tar per sync); changed and new skill and cache files are copied back when the environment is released (credential files are upload-only) |
| Approvals | Hermes skips its approval prompts and dangerous-command checks for commands and `execute_code` in the sandbox, as for its other cloud sandbox backends (your `approvals.deny` rules still apply to commands): it is an isolated microVM with no host paths mounted |
| Interrupt | Kills the command's process group in the sandbox; the sandbox keeps running |
| Secrets | `PRIZED_API_KEY` is stripped from every subprocess the agent spawns and never enters the sandbox |

In our tests on the live API, a background HTTP server and a counter loop survived every pause with the same PIDs.

### Background processes

A process the agent starts with `background: true` runs in the sandbox until it exits or is killed there, and a pause freezes it with the sandbox, same PID. On this backend, as on Hermes' other remote backends, two Hermes limits apply (seen with hermes-agent main in November 2026):

- Stopping it from Hermes (the `process` tool's kill, the session-end sweep, a gateway shutdown) sends `kill` to its wrapper shell only, so the process itself keeps running. Stop it with `kill` or `pkill` in the sandbox.
- Once Hermes releases the sandbox, it stops tracking the process: its `process` tools report it as exited, though it is still running.

While Hermes watches a background process it polls it every 2 seconds, and each poll is a command, so the sandbox stays awake (and billed) while Hermes runs and watches it.

## Settings

Optional, in `plugins.entries.prized.settings` (also in the Plugins hub). An invalid value stops the backend with a message instead of being ignored.

| Key | Default | Effect |
| --- | --- | --- |
| `scope` | `instance` | `instance` or `session`, above. Use `session` for a gateway that serves several people |
| `template` | `prized-base-v1` | Template for new sandboxes (`prized-agent-v1` adds Claude Code and Codex) |
| `vcpu` | `2` | 1 to 32 (2 at most on the free tier) |
| `memory_mb` | `4096` | 1024 to 131072 (4096 at most on the free tier) |
| `auto_pause_min` | `10` | Pause after this many idle minutes; `0` turns it off |
| `max_runtime_min` | account default | Minutes of running before the sandbox is deleted (at most 10080, 7 days) |
| `labels` | none | Extra string labels on each sandbox (keys must not start with `hermes`) |
| `api_url` | `https://api.prized.dev/api/v1` | API base URL |

Hermes' own `container_cpu` / `container_memory` are not used: their defaults (1 CPU, 5 GB) are above the free tier's 4 GB cap, so sizing comes from `vcpu` and `memory_mb`. `image` (the Docker image setting) does not apply either; `template` picks the image.

```bash
hermes config set plugins.entries.prized.settings.vcpu 4
hermes config set plugins.entries.prized.settings.memory_mb 8192
```

## Tests

Unit tests run against an in-process fake of the Prized API whose edge executes each command with the host's real bash, so they check what the scripts the backend sends actually do. From a hermes-agent checkout with its test environment:

```bash
cd /path/to/hermes-agent
scripts/run_tests.sh /path/to/integrations/hermes/tests/
```

## License

Apache-2.0
