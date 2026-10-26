# strands-prized

> **Coming 2026-11-05.** `strands-prized` is not on PyPI yet. The Prized Python SDK it uses (`prized`) is.

Run a [Strands Agents](https://strandsagents.com/) agent's shell commands, code and file edits in a [Prized](https://prized.dev/?via=strands) sandbox: a Firecracker microVM with its own kernel that pauses with its memory and running processes, and forks into live copies.

## Install

```sh
pip install strands-prized
export PRIZED_API_KEY=dcp_...
```

Make the key at https://prized.dev/dashboard/workspace/api-keys?via=strands with **Full access**, or **Custom** with **Sandboxes: Write**. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=strands. Python 3.10 or later, `strands-agents` 1.59 or later.

## Use it

```python
from strands import Agent
from strands_prized import PrizedSandbox

with PrizedSandbox() as sandbox:  # created on first use, deleted at the end
    agent = Agent(sandbox=sandbox)
    agent("Clone https://github.com/un33k/python-slugify, run its tests and fix any failure.")
    agent("Now add a test for an empty string.")  # same sandbox, files intact
```

The sandbox registers Strands' standard `sandbox_shell` and `sandbox_file_editor` tools. Vended tools that read the agent's sandbox, such as `python_repl` (in recent Strands releases), run there too. From async code, use `async with PrizedSandbox() as sandbox:` and `await agent.invoke_async(...)`.

## How it behaves

- **Lifecycle.** The first command creates the sandbox (`prized-base-v1`: Ubuntu 24.04, Python 3, Node.js 22, git), or `await sandbox.start()` does it up front. Closing it (`close()`, `await aclose()`, or leaving the `with` block, also inside a notebook) deletes a sandbox this object created, and the object can't be used again. With `on_close="pause"` the sandbox is paused instead, and `PrizedSandbox(sandbox_id=sandbox.sandbox_id)` picks it up later with its files and running processes; a paused sandbox keeps its disk, which counts toward your disk quota. An attached sandbox is kept unless `on_close` says otherwise.
- **Idle.** After `auto_pause_min` idle minutes (10 by default) the sandbox pauses itself, and the next command resumes it with everything intact. A sandbox that closing would delete is also deleted 60 minutes after it pauses (`auto_delete_min`), closed or not.
- **Commands** run under `bash` as the user `prized` (no sudo) in `/home/prized/work`, with outbound internet. Each call is a fresh shell. `pip install` works without root: every command gets `PIP_BREAK_SYSTEM_PACKAGES=1` and `~/.local/bin` on its `PATH`, so packages and their commands work. A `timeout` kills the command and raises `SandboxTimeoutError` with what it printed.
- **Output.** `execute_streaming()` yields output as the command prints it. `execute()` (what the shell tool uses) waits for the end and keeps up to 100 MiB of stdout and of stderr (64 Mi characters with no timeout or one over 4 minutes), and says so when it cut something off.
- **Files** anywhere the `prized` user can write. Reads use the file API (100 MiB per file) and fall back to the shell for symlinks and paths outside `/home/prized`. Writes go into the existing file, so it keeps its mode: an edited script stays executable.
- **More.** `await sandbox.fork(3)` returns 3 live copies (files, memory and processes) for parallel attempts; closing a copy deletes it. `await sandbox.pause()` pauses now. `sandbox.sandbox` is the `prized.AsyncSandbox` for anything else in the [Prized SDK](https://pypi.org/project/prized/).
- **Threads.** One `PrizedSandbox` can serve several agents on different threads (sync `agent(...)` calls run on a new event loop each); each loop gets its own connection to the same sandbox.
- **Gone.** If a sandbox this object created is gone (its run time ended, or it was deleted after pausing), the next call creates a fresh, empty one, logs a warning, and tells the model in that command's output.

Options: `sandbox_id`, `template` (`prized-base-v1`, or `prized-agent-v1` with Claude Code and Codex), `vcpu`, `memory_gb`, `disk_gb`, `env_vars`, `labels`, `working_dir`, `auto_pause_min`, `auto_delete_min`, `max_runtime_min`, `on_close`, `api_key`, `base_url`. With `sandbox_id`, only `working_dir`, `on_close`, `api_key` and `base_url` apply. Code in the sandbox, the model's included, can read `env_vars`: put there only what that code needs.

## Limits

On free credit, sandboxes run within the free tier: up to 10 at once, each up to 2 vCPU and 4 GB, with 60 minutes of running each. A top-up or a plan lifts that; a paid sandbox runs 24 hours by default, up to 7 days. No inbound ports, no custom images, no GPUs. Details: https://prized.dev/docs/sandboxes?via=strands

## Tests

```sh
pip install -e ".[dev]"
pytest                               # unit tests, no network
PRIZED_LIVE=1 pytest -m live -s      # live tests: real sandboxes, all deleted
pip install 'strands-agents[openai]' && python examples/fix_bug.py   # a real model fixes a seeded bug (OPENROUTER_API_KEY)
```

License: Apache-2.0.
