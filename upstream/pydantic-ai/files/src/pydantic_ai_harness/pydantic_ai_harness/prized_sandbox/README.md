# Prized Sandbox

Run your agent's commands and file edits in a [Prized](https://prized.dev) sandbox, a Firecracker microVM with its own kernel that pauses with its memory and running processes and forks into live copies, instead of on your machine.

[Source](https://github.com/pydantic/pydantic-ai/tree/main/src/pydantic_ai_harness/pydantic_ai_harness/prized_sandbox/)

> While Pydantic AI Harness is on 0.x releases, the API may change between minor releases; when it does, deprecation warnings and release-note migration guidance tell you (or your agent) exactly how to upgrade. See the [version policy](https://pydantic.dev/docs/ai/harness/#version-policy).

## Install

uv:

```bash
uv add "pydantic-ai-harness[coder,prized,anthropic]"
```

pip:

```bash
pip install "pydantic-ai-harness[coder,prized,anthropic]"
```

The `coder` extra is there because the examples use `Coder`; the sandbox itself doesn't need it. The `anthropic` extra is there because the examples use an Anthropic model; swap it for your model provider's extra. Then set `PRIZED_API_KEY` to a key from [prized.dev/dashboard/workspace/api-keys](https://prized.dev/dashboard/workspace/api-keys) with **Sandboxes: Write** (new workspaces get free credit), and `ANTHROPIC_API_KEY` for the examples.

## Quick start

```python {names="defined"}
from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.prized_sandbox import PrizedSandbox

agent = Agent('anthropic:claude-opus-5-5', capabilities=[PrizedSandbox(), Coder()])
result = agent.run_sync('Clone https://github.com/pydantic/pydantic-ai and summarize how capabilities work.')
```

`Coder`'s shell and file tools now run in the sandbox, not on your machine. With `Coder`, `RepoContext` creates the sandbox when the run starts, even without a tool call; use `Coder(repo_context=False)` for lazy creation. The sandbox keeps running after the run ends; after `auto_pause_min` idle minutes (10 by default) it pauses itself with its memory and running processes, and stops using compute until the next command resumes it. See [Clean up](#clean-up).

The default template, `prized-base-v1`, is Ubuntu 24.04 with Python 3.12, Node.js, git, and ripgrep (`rg`), which `Coder` uses for searches; `PrizedSandbox(template='prized-agent-v1')` adds Claude Code and Codex. Commands run as the non-root user `prized` in `/home/prized/work`. `PrizedSandbox(vcpu=4, memory_mb=8192)` picks a larger sandbox; the template's default is used when unset (2 vCPU and 4 GB on Prized's free tier).

## Continue in the same sandbox

```python {names="defined"}
from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.prized_sandbox import PrizedSandbox

agent = Agent('anthropic:claude-opus-5-5', capabilities=[PrizedSandbox(), Coder()])
result = agent.run_sync('Clone https://github.com/pydantic/pydantic-ai and summarize how capabilities work.')

followup = agent.run_sync(
    'Which capability would you add next, and where would it live?',
    message_history=result.all_messages(),
)
```

The follow-up run finds the sandbox in the message history and works in it, so the clone is still there, and so is anything the first run left running, even if the sandbox paused in between. Without the history, a run starts a new sandbox. If the sandbox has been deleted, the run raises `WorkspaceUnavailableError` instead of starting over in an empty one.

## Pause, and fork a sandbox

```python {names="defined"}
from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.prized_sandbox import PrizedSandbox

sandbox = PrizedSandbox()
agent = Agent('anthropic:claude-opus-5-5', capabilities=[sandbox, Coder()])


async def try_two_fixes() -> None:
    result = await agent.run('Clone https://github.com/un33k/python-slugify, install it and run its tests.')
    ref = result.workspace.ref
    assert ref is not None
    copy = await sandbox.fork(ref)  # files, memory and running processes, as they are now
    try:
        await agent.run('Fix the failing test by changing the code.', workspace=ref)
        await agent.run('Fix the failing test by changing the test.', workspace=copy)
    finally:
        await sandbox.destroy(copy)
    await sandbox.pause(ref)  # memory kept; the next run that attaches resumes it
```

`fork(ref)` copies a sandbox live, in about a second: the copy has its files, memory and running processes, and the two change independently after. `pause(ref)` stops the compute bill while keeping the sandbox's memory, so a dev server or a half-finished build is still there when a later run attaches. For more than one copy at a time (`fork(count)`), preview URLs for a port, or a longer run time, `await PrizedSandbox().backend(ref).get_sandbox()` returns a [`prized.AsyncSandbox`](https://pypi.org/project/prized/) handle; close it with `await handle.close()` when you are done (the sandbox keeps running).

## Clean up

A run never deletes its sandbox. Delete it when you are done with `await PrizedSandbox().destroy(ref)`, which works in any state and returns quietly for a sandbox that is already gone:

```python {names="defined"}
from pydantic_ai.workspaces import WorkspaceRef
from pydantic_ai_harness.prized_sandbox import PrizedSandbox


async def delete_sandbox(ref: WorkspaceRef) -> None:
    await PrizedSandbox().destroy(ref)
```

Prized also deletes a sandbox when its run time ends (`max_runtime_min`: 24 hours of running by default, 60 minutes on the free tier; paused time doesn't count), or after `auto_delete_min` minutes paused. Every sandbox `PrizedSandbox` creates carries the label `created-by=pydantic-ai`, plus any `labels=` you pass, so `prized sandbox ls` and the SDK's `AsyncSandbox.delete_by_labels(...)` find leftovers.

## Security model

The sandbox is a Firecracker microVM with its own kernel; commands run in it as the unprivileged `prized` user and can't reach the machine that runs the agent. Nothing from your environment is copied in: `env=` is the only way variables get there, and anything you put in it, a key included, is readable by the agent's commands. `PRIZED_API_KEY` stays with the agent process and is never sent into the sandbox. Outbound internet is open, so the agent can clone and install. Attaching follows the `WorkspaceRef` in the message history: a ref only reaches sandboxes in the workspace of the key you use.

## Configuration

| Parameter | Default | What it does |
| --- | --- | --- |
| `template` | `'prized-base-v1'` | Template a new sandbox starts from (`prized-agent-v1` adds Claude Code and Codex) |
| `vcpu`, `memory_mb`, `disk_gb` | Template's | Shape of a new sandbox |
| `auto_pause_min` | `10` | Idle minutes before a new sandbox pauses itself, memory kept; `0` turns it off |
| `auto_delete_min` | `None` | Minutes paused before a new sandbox deletes itself |
| `max_runtime_min` | `None` | Minutes of running before a new sandbox is deleted (Prized's default: 24 hours) |
| `working_dir` | `None` | Absolute directory commands start in; created on a new sandbox |
| `env` | `None` | Environment variables every command gets |
| `labels` | `None` | Labels for a new sandbox, added to `created-by=pydantic-ai` |
| `api_key`, `base_url`, `edge_url` | From the environment / Prized's | Connection settings; `PRIZED_API_KEY` when `api_key` is unset |

A command with a `timeout` of up to 240 seconds runs as one call, which keeps the first 4 MiB of each stream; a longer or unbounded one runs detached and is polled until it ends. Past what is kept, the command raises `WorkspaceOutputLimitError`, so redirect large output to a file. A timeout or a cancelled run stops the command's whole process group; a process it started in the background (`server &`) keeps running, so a dev server is still there for the next command. Reading a file under `/home/prized` is one call to Prized's file API; writes and the other file operations go through the shell as `prized`, so they honor the sandbox's permissions and reach every path that user can.

## Telemetry

`PrizedSandbox` emits no spans of its own. Core's [instrumentation](https://pydantic.dev/docs/ai/capabilities/instrumentation/) records the sandbox on the agent run span as `pydantic_ai.workspace.provider` and `pydantic_ai.workspace.id`, and each command or file operation a tool makes runs inside that tool call's span. Creating or forking a sandbox logs its id at `INFO` (`Created Prized sandbox <id>`) on the `pydantic_ai_harness.prized_sandbox._backend` logger.

## API reference

::: pydantic_ai_harness.prized_sandbox.PrizedSandbox

::: pydantic_ai_harness.prized_sandbox.PrizedSandboxBackend
