# openai-agents-prized

[Prized Sandboxes](https://prized.dev/docs/sandboxes?via=openai-agents) as a sandbox client for the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/sandbox/clients/). A `SandboxAgent` runs its shell commands and file edits in a Prized sandbox, a Linux microVM that is deleted when the run ends.

## Install

```bash
pip install openai-agents-prized
export PRIZED_API_KEY=dcp_...
export OPENAI_API_KEY=sk-...
```

Make the Prized key at [prized.dev/dashboard/workspace/api-keys](https://prized.dev/dashboard/workspace/api-keys?via=openai-agents) with **Full access**, or **Custom** with **Sandboxes: Write**. Python 3.10 or later, `openai-agents` 0.23.1 or later, below 0.26.

## Run an agent

```python
import asyncio

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig

from openai_agents_prized import PrizedSandboxClient, PrizedSandboxClientOptions


async def main() -> None:
    agent = SandboxAgent(name="coder", instructions="Work in the sandbox with your tools.")
    async with PrizedSandboxClient() as client:
        run_config = RunConfig(
            sandbox=SandboxRunConfig(
                client=client,
                options=PrizedSandboxClientOptions(vcpu=2, memory_mb=4096),
            )
        )
        result = await Runner.run(
            agent,
            "Write fizzbuzz.py, run it for 15, and show me the output.",
            run_config=run_config,
        )
    print(result.final_output)


asyncio.run(main())
```

The runner creates a sandbox for the run and deletes it when the run ends, also after a tool error, a timeout or a cancellation. Leaving `async with` closes the client's connections and waits for any cleanup still running.

The agent's `exec_command`, `write_stdin`, and `apply_patch` tools run in the sandbox as the user `prized`, in `/home/prized/work`. A manifest on the SDK's default root (`/workspace`) is placed there too.

## Keep one sandbox across runs

Create the session yourself and pass it to each run. The runner uses it and leaves it running:

```python
async with PrizedSandboxClient() as client:
    session = await client.create(options=PrizedSandboxClientOptions(template="prized-agent-v1"))
    try:
        async with session:
            run_config = RunConfig(sandbox=SandboxRunConfig(session=session))
            await Runner.run(agent, "Clone the repo and run its tests.", run_config=run_config)
            await Runner.run(agent, "Fix the failing test.", run_config=run_config)
    finally:
        await client.delete(session)
```

Leaving `async with session` deletes the sandbox, or pauses it with `pause_on_exit`. `client.delete(session)` also covers a session that failed to start, and does nothing when the sandbox is already released.

## Options

`PrizedSandboxClientOptions` maps to the Sandbox API's [create](https://prized.dev/docs/sandboxes-api?via=openai-agents#create):

| Option | Default | Effect |
| --- | --- | --- |
| `template` | `prized-base-v1` | `prized-agent-v1` adds Claude Code and Codex |
| `vcpu`, `memory_mb`, `disk_gb` | The template's | The sandbox's shape |
| `env_vars`, `labels` | None | Set for every command; yours to filter on |
| `auto_pause_min`, `auto_delete_min`, `max_runtime_min` | Off, off, 1,440 (60 on the free tier) | Automatic pause and delete |
| `pause_on_exit` | `False` | Pause instead of delete at the end of a run, so `resume()` continues in the same sandbox with its files and installs (the SDK ends the run's commands before the pause) |
| `timeouts` | | `PrizedSandboxTimeouts`: per-command default, startup, and archive timeouts |

`PrizedSandboxClient(api_key=..., api_url=...)` reads `PRIZED_API_KEY` and `PRIZED_API_URL` when they are not passed.

## Pause and resume

The client implements the SDK's session lifecycle: `RunState` round trips, snapshots, and `resume()`. With `pause_on_exit=True`, a paused sandbox keeps memory and disk for the price of storage, and the resumed run reuses it without restoring anything. Without it, the sandbox is deleted and a resumed run starts a new one and restores the workspace from the session's snapshot.

The saved session state holds the sandbox's id, the options above, and the SDK's own fields (manifest, snapshot). This release resumes state saved by 0.1.0, and its tests check that.

## Moving from Runloop

`openai-agents` removed `RunloopSandboxClient`. Swap the client and options; the agent stays the same:

| Runloop | Prized |
| --- | --- |
| `RunloopSandboxClient()` | `PrizedSandboxClient()` |
| `RunloopSandboxClientOptions(blueprint_name=...)` | `PrizedSandboxClientOptions(template=...)` |
| `env_vars`, `pause_on_exit` | Same names |
| `metadata` | `labels` |
| `exposed_ports`, tunnels, gateways, MCP, managed secrets, mounts | Not supported |

Runloop session state cannot be resumed on Prized; start new runs.

## Limits

- **Isolation.** Each session gets its own sandbox, a Firecracker microVM. Nothing runs on your machine, and nothing falls back to it.
- **No root.** Commands run as `prized` without sudo, so manifest users other than `prized`, manifest groups, and mounts are refused before a sandbox is created. Every other manifest entry works: files, directories, local files and directories, and git repos.
- **No inbound network.** Sandboxes reach the internet, but nothing reaches them: exposed ports are refused (`ExposedPortUnavailableError`).
- **Commands.** A command that outlives `exec_command`'s yield keeps running, and `write_stdin` polls it or types into it (with `tty=True`, Ctrl-C included). It runs until it exits or the session ends, unless the tool call passes a `timeout`. A command run with `exec` takes up to an hour (`PrizedSandboxTimeouts.exec_timeout_unbounded_s`) and keeps the first 4 MiB of its output and of its errors.
- **Snapshots.** Symlinks are kept as links, so a virtualenv in the workspace comes back working in a replacement sandbox; hard-linked files come back as separate copies.
- **Run time.** 24 hours of running by default, up to 7 days with `max_runtime_min`; on the free tier 60 minutes, 2 vCPU and 4 GB.
- **Keys.** Code in the sandbox, the model's included, can read `env_vars`. Keep your Prized key out of them.

Refusals from the API raise `PrizedAPIError` with the API's [error code](https://prized.dev/docs/sandboxes-api?via=openai-agents#errors); sandbox failures inside a run surface as the SDK's own `SandboxError` types.

## When something fails

A failed or cancelled create deletes the sandbox it made, and a failed resume pauses the sandbox it woke; if even that cannot reach the API, a warning names the sandbox. An answer that never came is settled by reading the sandbox, so a pause that landed is never followed by a delete. `close()` waits for cleanup a cancellation left running. A command whose answer is lost is never sent again, and its error says which of three it was: it never reached the sandbox, it may or may not have run, or it ran but the answer could not be read.

## Development

The source is in [prized-dev/integrations](https://github.com/prized-dev/integrations/tree/main/openai-agents). From a clone:

```bash
scripts/test.sh openai-agents            # lint, types and offline tests in a fresh virtualenv, as CI runs them
FLOOR=1 scripts/test.sh openai-agents    # the same against openai-agents 0.23.1, the oldest allowed

cd openai-agents
uv sync
uv run pytest                                          # offline, against an in-process fake of the API
PRIZED_LIVE_TESTS=1 PRIZED_API_KEY=dcp_... uv run pytest -m live   # the SDK's lifecycle test on real sandboxes
PRIZED_API_KEY=dcp_... uv run python scripts/live_agent.py         # a coding agent end to end, scripted model turns
```

The offline tests include the SDK's own sandbox lifecycle scenario and the `Runner` checks from its [provider guide](https://github.com/openai/openai-agents-python/blob/main/docs/sandbox/custom-providers.md): runner-owned and application-owned sessions, resume from saved `RunState`, and cleanup after a tool error, a timeout, a cancellation and a failed start. [FAILURES.md](https://github.com/prized-dev/integrations/blob/main/openai-agents/FAILURES.md) maps every failure case to its test.

Apache-2.0.
