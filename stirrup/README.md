# stirrup-prized

> **Not on PyPI yet.** Install it from this repo. The Prized Python SDK it uses (`prized`) is on PyPI. This version is tested offline (see Tests) and hasn't yet run a Stirrup agent against live sandboxes.

Run [Stirrup](https://github.com/ArtificialAnalysis/Stirrup) agents (Artificial Analysis's agent harness) in [Prized](https://prized.dev/?via=stirrup) sandboxes: Firecracker microVMs that pause with their memory and fork into live copies. `PrizedCodeExecToolProvider` is a drop-in for Stirrup's `E2BCodeExecToolProvider`: the same keywords, the same `code_exec` tool, the same error kinds.

## Install

```sh
pip install "stirrup-prized @ git+https://github.com/prized-dev/integrations#subdirectory=stirrup"
export PRIZED_API_KEY=dcp_...
```

Make the key at https://prized.dev/dashboard/workspace/api-keys?via=stirrup with **Full access**, or **Custom** with **Sandboxes: Write**. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=stirrup. Python 3.12 or later, Stirrup 0.2.1 or later (below 0.3).

## Use it

Swap the provider; nothing else changes:

```python
from stirrup import Agent
from stirrup_prized import PrizedCodeExecToolProvider

code_exec = PrizedCodeExecToolProvider(timeout=3600)  # was E2BCodeExecToolProvider(timeout=3600)
agent = Agent(client=client, name="agent", tools=[code_exec], max_turns=50)

async with agent.session(output_dir="output") as session:
    await session.run("Chart the first 50 Fibonacci numbers and save it as fibonacci.png")
```

`examples/fibonacci.py` is Stirrup's E2B example on Prized.

To start every run from the same prepared state (packages installed, data in place), prepare one sandbox with the `prized` SDK, pause it, and fork it per run:

```python
from prized import AsyncSandbox

parent = await AsyncSandbox.create(template="prized-base-v1", max_runtime_min=1440)
await parent.exec("python3 -m venv ~/.venv && ~/.venv/bin/pip install pandas matplotlib", timeout=600)
await parent.pause()

code_exec = PrizedCodeExecToolProvider(fork_from=parent.id)  # each run gets a live copy, deleted at the end
```

## What works now, and what waits for Docker mode (2026-11-09)

| | Now | From 2026-11-09 (Docker mode) |
| --- | --- | --- |
| Commands, files, uploads, outputs, images | Yes | Yes |
| Commands over 240 s (up to hours) | Yes: run detached and polled | Yes |
| `fork_from` a prepared sandbox | Yes | Yes |
| `network=False` / `allow_internet_access=False` | Raises `UnsupportedOptionError` | Planned: container with no network |
| `image=` / your own template | Raises `UnsupportedOptionError` | Planned: run your image |
| User `user`, home `/home/user`, sudo | No: user `prized`, home `/home/prized`, no sudo | Planned, in your image |

Docker mode isn't in this version yet. Until it ships, an option this backend can't honour raises before any sandbox is made. It never runs an offline task online.

## How it behaves

- **Commands** run under `bash` as the user `prized` in `/home/prized/work`, with outbound internet. If a command deletes that directory, it is made again. `shell_timeout` (Stirrup's 300 s by default) applies to each one, and `0` means no limit. Commands over 240 s run detached and are polled, so a long build never holds a connection open. A command that runs past its timeout returns `error_kind="timeout"` with the output it printed so far.
- **Errors.** A non-zero exit is a result, not an error. A refused command is `invalid_argument`. If the sandbox doesn't answer (network, rate limit, 5xx), the result is `error_kind="sandbox_unreachable"`; if it answers but can't run or follow the command (a full disk, say), `sandbox_error`. Either way the run goes on, because Stirrup's `Agent` ends the whole run on any exception from a tool. A sandbox that's gone, or a refused key, balance or quota, still raises.
- **Lifetime.** `timeout` (seconds, 600 by default as in E2B) becomes the sandbox's run time, rounded up to whole minutes of running. Leaving the `async with` deletes it, even when the task is cancelled. A sandbox that pauses is deleted 60 minutes later unless you set `auto_delete_min`, so a crashed run doesn't leave it behind. Label each run (`sandbox_kwargs={"metadata": {"run": run_id}}`), and `AsyncSandbox.delete_by_labels({"run": run_id})` sweeps whatever a crash left.
- **Files.** Relative paths resolve against `/home/prized/work`. Files under `/home/prized` (up to 100 MiB each) go through the files API. Elsewhere, or through a symlink, they're copied via `/home/prized` first. Output files must resolve under `/home/prized`, and keep their path relative to `/home/prized/work` (or `/home/prized`).
- **Options.** `template` (`prized-base-v1`: Ubuntu 24.04, Python 3.12, Node 22, git; or `prized-agent-v1`, which adds Claude Code and Codex), `vcpu`, `memory_gb`, `disk_gb`, `fork_from`, `request_timeout` (seconds to wait for the sandbox to start), `create_gate` (`asyncio.Semaphore(8)` caps starts at 8), `allowed_commands`. In `sandbox_kwargs`, E2B's `envs` and `metadata` become env vars and labels. Prized's own `env_vars`, `labels`, `auto_pause_min`, `auto_delete_min`, `memory_mb`, `idempotency_key`, `api_key`, `base_url` and `http_client` pass through. Any other key raises `TypeError`.
- **More.** `provider.sandbox` is the `prized.AsyncSandbox` while it runs, for `pause()`, `fork()` and the rest of the [Prized SDK](https://pypi.org/project/prized/).

## Limits

- Prompts that name `/home/user` (E2B's home) don't match `/home/prized` until Docker mode.
- Offline benchmarks (no outbound network) can't run on this version.
- An E2B template name doesn't carry over. Install what a task needs, or prepare a sandbox and use `fork_from`.
- A copy made with `fork_from` doesn't take `timeout`: the `prized` 0.1.1 SDK can't set a fork's run time, so a copy that a crashed run leaves behind keeps running until its own run time ends, which can be hours. Label runs and sweep them, as above.
- Workspace quotas: 50 running sandboxes, 200 vCPU and 400 GB of memory across them. The edge takes 48 concurrent requests per API key, and a command under 240 s holds one for as long as it runs. For hundreds of parallel trials, ask team@prized.dev to raise the quotas, or spread runs over several keys.
- On free credit: 10 sandboxes at once, each up to 2 vCPU and 4 GB with 60 minutes of running. A paid sandbox runs 24 hours by default, up to 7 days.
- No GPUs, no inbound ports. Details: https://prized.dev/docs/sandboxes?via=stirrup

## Tests

```sh
pip install -e ".[dev]"
pytest
```

The tests run offline and don't install Stirrup. `tests/stub` stands in for the part of Stirrup's interface the backend uses, and `tests/fakes.py` for the Prized SDK (its commands run in a local `bash`). `tests/test_real_sdk.py` drives the real `prized` SDK against canned API answers. To run the same tests against an installed Stirrup instead of the stand-in, set `STIRRUP_PRIZED_REAL_STIRRUP=1`.

License: Apache-2.0.
