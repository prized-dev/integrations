# agent-framework-prized

> **Coming 2026-11-05.** `agent-framework-prized` is not on PyPI yet. The Prized Python SDK it uses (`prized`) is.

Run a [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) agent's shell commands in a [Prized](https://prized.dev/?via=agent-framework) sandbox: a Firecracker microVM with its own kernel that pauses with its memory and running processes, and forks into live copies. `PrizedShellTool` has the same surface as the framework's `DockerShellTool` and implements its `ShellExecutor` protocol.

## Install

```sh
pip install agent-framework-prized
export PRIZED_API_KEY=dcp_...
```

Make the key at https://prized.dev/dashboard/workspace/api-keys?via=agent-framework with **Full access**, or **Custom** with **Sandboxes: Write**. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=agent-framework. Python 3.10 or later, `agent-framework-core` 1.19 or later, `agent-framework-tools` 1.0.0b260918 or later.

## Use it

```python
import asyncio

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from agent_framework_prized import PrizedShellTool


async def main() -> None:
    async with PrizedShellTool(approval_mode="never_require") as shell:  # the sandbox is the boundary
        agent = Agent(client=OpenAIChatClient(model="gpt-5.4-mini"), tools=[shell.as_function()])
        print(await agent.run("Clone https://github.com/un33k/python-slugify, run its tests and fix any failure."))


asyncio.run(main())
```

Any chat client works: `as_function()` is an ordinary function tool. To tell the model where it is (bash on Ubuntu, the working directory, which CLIs exist), add `context_providers=[ShellEnvironmentProvider(shell, shell.environment_options())]` (from `agent_framework.tools`). With `create_harness_agent`, pass `shell_executor=shell, shell_environment_provider_options=shell.environment_options()`; the harness wires the shell only for clients with a native shell tool, such as `OpenAIChatClient`, so with other clients pass `tools=[shell.as_function()]` instead.

## Files for the harness

`PrizedFileStore(shell)` is an `AgentFileStore` over the same sandbox, so the harness's file tools and the shell see the same files:

```python
from agent_framework import create_harness_agent
from agent_framework_prized import PrizedFileStore

agent = create_harness_agent(client, shell_executor=shell, file_access_store=PrizedFileStore(shell))
```

Paths are relative to the shell's working directory (or `root=`) and can't leave it: absolute paths, `..` and symlinks that lead out are refused. A write lands whole or not at all and keeps the file's mode, so an edited script stays executable. The harness asks for approval before each file operation unless you pass `file_access_disable_readonly_tool_approval=True` and `file_access_disable_write_tool_approval=True`.

## How it behaves

- **Lifecycle.** The first command creates the sandbox (`prized-base-v1`: Ubuntu 24.04, Python 3.12 as `python3`, Node.js 22, git), or `await shell.start()` (also `async with`) does it up front. `await shell.close()` deletes a sandbox the tool created and keeps one it attached to (`sandbox_id=...`); `on_close="pause"` or `"keep"` changes that. Like the framework's own tools, a closed tool starts again on its next command: in the same sandbox if it was paused or kept, otherwise in a new one.
- **Session.** In the default `mode="persistent"`, `cd`, variables (exported or not), shell functions and `shopt` options carry over between commands, as in one long-lived bash: a virtualenv you activate stays active. The session lives in the sandbox, so it survives pause and resume, and `PrizedShellTool(sandbox_id=...)` continues it. A command killed by its timeout leaves the session as it was. In `mode="stateless"` every command starts fresh in `working_dir`. Files persist either way. On one event loop, the commands of a persistent session run one at a time.
- **Commands** run under `bash` as the user `prized` (no sudo) in `/home/prized/work` (made again if a command removes it), with outbound internet. `pip install` works without root: every command gets `PIP_BREAK_SYSTEM_PACKAGES=1` and `~/.local/bin` on its `PATH`. A command is stopped after `timeout` seconds (120 by default, `None` for no limit; the framework's local and Docker tools default to 30), with every process it started, and returns what it printed with `timed_out=True`. Commands of any length work.
- **Output.** Up to `max_output_bytes` (64 KiB) of stdout and of stderr reach the model: the start and the end, with the middle cut. The sandbox keeps up to 100 MiB of each (64 Mi characters for a streamed command, one without a time limit or one over 4 minutes), and says so when it cut more. With `prized` 0.1.1 or later it also says when a full sandbox disk cut the output short, and a command that ran but whose output couldn't be read back returns its exit code and where the output is kept. `on_output=lambda stream, text: ...` sees output as it is printed; an error in it is logged, not raised.
- **Background processes.** Start them with `nohup CMD > out.log 2>&1 &`: they keep running between commands, and the call returns at once. One that keeps the command's output open makes the call wait 5 seconds.
- **Approval.** `approval_mode` defaults to `"always_require"`, as in `DockerShellTool`; `"never_require"` runs the model's commands unattended, the sandbox being the boundary. `policy=ShellPolicy(...)` and `on_command=` work as in the framework's tools.
- **Idle.** After `auto_pause_min` idle minutes (10 by default) the sandbox pauses itself, and the next command resumes it with everything intact. A sandbox that closing would delete is also deleted 60 minutes after it pauses (`auto_delete_min`), closed or not.
- **More.** `await shell.fork(3)` returns 3 started tools on live copies (files, memory, processes and shell session) for parallel attempts; closing a copy deletes it. `await shell.pause()` pauses now. `shell.sandbox` is the `prized.AsyncSandbox` for anything else in the [Prized SDK](https://pypi.org/project/prized/), such as file uploads.
- **Threads.** One tool can serve several event loops and threads (each loop gets its own connection to the same sandbox), though the framework intends one shell per conversation.
- **Gone.** If a sandbox the tool created is gone (its run time ended, or it was deleted after pausing), the next command creates a fresh, empty one, logs a warning, and tells the model in that command's output. An attached sandbox that is gone raises `ShellExecutionError`.

Options: `sandbox_id`, `template` (`prized-base-v1`, or `prized-agent-v1` with Claude Code and Codex), `vcpu`, `memory_gb`, `disk_gb`, `labels`, `mode`, `working_dir`, `env`, `timeout`, `max_output_bytes`, `policy`, `approval_mode`, `on_command`, `on_output`, `auto_pause_min`, `auto_delete_min`, `max_runtime_min`, `on_close`, `api_key`, `base_url`. With `sandbox_id`, `template`, `vcpu`, `memory_gb`, `disk_gb`, `labels`, `auto_pause_min`, `auto_delete_min` and `max_runtime_min` don't apply. `env` is applied to every command, and is never saved with the session unless a command changes a variable. Code in the sandbox, the model's included, can read `env`: put there only what that code needs.

## Limits

On free credit, sandboxes run within the free tier: up to 10 at once, each up to 2 vCPU and 4 GB, with 60 minutes of running each. A top-up or a plan lifts that; a paid sandbox runs 24 hours by default, up to 7 days. No inbound ports, no custom images, no GPUs. Details: https://prized.dev/docs/sandboxes?via=agent-framework

## Tests

```sh
pip install -e ".[dev]"
pytest                               # unit tests, no network
PRIZED_LIVE=1 pytest -m live -s      # live tests: real sandboxes, all deleted
pip install agent-framework-openai && python examples/fix_bug.py   # a real model fixes a seeded bug (OPENROUTER_API_KEY)
```

License: Apache-2.0.
