# prized-adk

> **Coming 2026-11-05.** `prized-adk` is not on PyPI yet. The Prized Python SDK it uses (`prized`) is.

Run a [Google ADK](https://google.github.io/adk-docs/) agent's shell commands, files and code in a [Prized](https://prized.dev/?via=adk) sandbox: a Firecracker microVM with its own kernel that pauses with its memory, and forks into live copies.

- `PrizedEnvironment` plugs into ADK's `EnvironmentToolset` (the `Execute`, `ReadFile`, `EditFile` and `WriteFile` tools), like ADK's built-in E2B and Daytona environments.
- `PrizedCodeExecutor` runs the code blocks an `LlmAgent` writes (`code_executor=`).

## Install

```sh
pip install prized-adk
export PRIZED_API_KEY=dcp_...
```

Make the key at https://prized.dev/dashboard/workspace/api-keys?via=adk with **Full access**, or **Custom** with **Sandboxes: Write**. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=adk. It can also install the Prized skill with `npx skills add https://prized.dev -g -y` (it should ask you first), so it knows when and how to use Prized the next time you ask for a sandbox. Python 3.10 or later, `google-adk` 1.30 or later.

## Use it

```python
from google.adk.agents import LlmAgent
from google.adk.tools.environment import EnvironmentToolset
from prized_adk import PrizedEnvironment

root_agent = LlmAgent(
    name="coder",
    model="gemini-flash-latest",
    instruction="Write, run and fix code in your workspace.",
    tools=[EnvironmentToolset(environment=PrizedEnvironment())],
)
```

Run it with `adk run` or `adk web`, or your own `Runner`. Closing the runner (`await runner.close()`) closes the toolset, which deletes the sandbox.

To run the model's code blocks instead:

```python
from prized_adk import PrizedCodeExecutor

calc_agent = LlmAgent(name="calc", model="gemini-flash-latest", code_executor=PrizedCodeExecutor())
```

## How it behaves

- **Lifecycle.** The first request to the model creates the sandbox (`prized-base-v1`: Ubuntu 24.04, Python 3, Node.js 22, git), when ADK sets up the toolset. `close()` deletes a sandbox the environment created. With `on_close="pause"` it is paused instead: memory, disk and running processes are kept, `initialize()` picks it up again (an `adk web` reload), and so does `PrizedEnvironment(sandbox_id=env.sandbox_id)`; a paused sandbox keeps its disk, which counts toward your disk quota. An attached sandbox is kept unless `on_close` says otherwise.
- **Idle.** After `auto_pause_min` idle minutes (10 by default) the sandbox pauses itself, and the next command resumes it with everything intact. A sandbox that `close()` would delete is also deleted 60 minutes after it pauses (`auto_delete_min`), closed or not.
- **Commands** run under `bash` as the user `prized` (no sudo) in `/home/prized/work`, with outbound internet. `pip install` works without root: every command gets `PIP_BREAK_SYSTEM_PACKAGES=1` and `~/.local/bin` on its `PATH`. A command keeps up to 100 MiB of stdout and of stderr (64 Mi characters with no timeout or one over 4 minutes), and stderr ends with a note when the output was cut.
- **Files** anywhere the `prized` user can write. Reads use the file API (100 MiB per file) and fall back to the shell for symlinks and paths outside `/home/prized`. Writes go into the existing file, so it keeps its mode: an edited script stays executable.
- **More.** `await env.fork(3)` returns 3 live copies (files, memory and processes) as new environments; closing a copy deletes it. `await env.pause()` pauses now. `env.sandbox` is the `prized.AsyncSandbox` for anything else in the [Prized SDK](https://pypi.org/project/prized/).
- **Threads.** One environment can serve several threads and event loops at once (sync `Runner.run` uses a new loop per call); each loop gets its own connection to the same sandbox.
- **Gone.** If a sandbox the environment created is gone (its run time ended, or it was deleted after pausing), the next call creates a fresh, empty one, logs a warning, and tells the model in that command's output.
- **Code executor.** Each execution gets a fresh sandbox, deleted afterwards. `keep_sandbox=True` reuses one, so files and installs carry over (Python variables do not), and starts a new one if it is gone; `Runner.close()` doesn't close code executors, so call `executor.close()` to delete it. Input files are written to the working directory, and files the code creates there come back as artifacts. The template's Python has only the standard library, so `optimize_data_file=True` (which uses pandas) needs pandas installed first.

Options on `PrizedEnvironment`: `template` (`prized-base-v1`, or `prized-agent-v1` with Claude Code and Codex), `sandbox_id`, `vcpu`, `memory_gb`, `disk_gb`, `env_vars`, `labels`, `working_dir`, `auto_pause_min`, `auto_delete_min`, `max_runtime_min`, `on_close`, `api_key`, `base_url`. With `sandbox_id`, only `working_dir`, `on_close`, `api_key` and `base_url` apply. Code in the sandbox, the model's included, can read `env_vars`: put there only what that code needs.

## Limits

On free credit, sandboxes run within the free tier: up to 10 at once, each up to 2 vCPU and 4 GB, with 60 minutes of running each. A top-up or a plan lifts that; a paid sandbox runs 24 hours by default, up to 7 days. No inbound ports, no custom images, no GPUs. Details: https://prized.dev/docs/sandboxes?via=adk

## Tests

```sh
pip install -e ".[test]"
pytest                                  # unit tests, no network
PRIZED_LIVE=1 pytest -m live -s         # live tests: real sandboxes, all deleted
pip install litellm && python examples/fix_bug.py   # a real model fixes a seeded bug (OPENROUTER_API_KEY)
```

License: Apache-2.0.
