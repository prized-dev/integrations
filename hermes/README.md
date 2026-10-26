# Prized for Hermes Agent

Prized terminal backend for [Hermes Agent](https://github.com/NousResearch/hermes-agent): Hermes runs its terminal, file and `execute_code` commands in a [Prized sandbox](https://prized.dev/docs/sandboxes), a Linux microVM that **pauses between turns with its memory**. A dev server, a watcher or a REPL the agent started is still running at the next command, in the same session or the next one.

It registers `terminal.backend: prized` through the pluggable terminal-backend extension point (hermes-agent PR #94400), so it appears in `hermes setup`, `hermes status` / `hermes doctor`, the dashboard picker and subprocess secret stripping with no change to Hermes core. It needs no SDK: it calls the [Prized HTTP API](https://prized.dev/docs/sandboxes-api) with httpx, which Hermes already ships.

## Install

```bash
hermes plugins install prized-dev/integrations/hermes --enable
hermes config set terminal.backend prized
```

Create an API key on the [Prized API keys page](https://prized.dev/dashboard/workspace/api-keys) (Full access, or Custom with **Sandboxes: Write**) and save it in `~/.hermes/.env` as `PRIZED_API_KEY=dcp_...`. `hermes doctor` shows a "Prized API key" row and the settings in effect.

## Behaviour

| | |
| --- | --- |
| Machine | Ubuntu 24.04 microVM from `prized-base-v1` (git, Python 3, Node.js 22, build tools). User `prized` without sudo, commands start in `/home/prized/work` |
| Between turns | When Hermes releases the environment (its idle reaper, session end, exit), the sandbox is **paused**: memory and disk kept, compute not billed. The next command resumes it |
| Sharing | `scope: instance` (default): one sandbox per Hermes home, shared by every session, cron job and subagent, so it is one machine for your agent. `scope: session`: one sandbox per session |
| Lifetime | A sandbox is deleted when its run time is used up: 24 hours of running by default, up to 7 days on a paid plan (each resume restarts the clock), 60 minutes in all on the free tier. The next command then starts a fresh sandbox and its result carries `environment_recreated`, so the model knows processes and files there are gone |
| Safety net | Every sandbox auto-pauses after 10 idle minutes, so a crashed Hermes never leaves one running |
| Files | Credentials, skills and cache under `~/.hermes` are synced in (one tar per sync) and changed files are pulled back when the environment is released |
| Interrupt | Kills the command's process group in the sandbox; the sandbox keeps running |
| Secrets | `PRIZED_API_KEY` is stripped from every subprocess the agent spawns and never enters the sandbox |

In our tests on the live API, a background HTTP server and a counter loop survived every pause with the same PIDs.

One thing to know: Hermes polls a background process it started (`background: true`) every 2 seconds for output. Each poll is a command, so while Hermes is running and watching such a process, the sandbox stays awake. Once Hermes exits or stops watching, the sandbox pauses and the process is frozen until the next command.

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
