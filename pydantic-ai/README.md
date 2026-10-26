# pydantic-ai-prized

> **Coming 2026-11-05.** `pydantic-ai-prized` and the Prized Python SDK it uses (`prized`) are not on PyPI yet.

Run a [Pydantic AI](https://pydantic.dev/docs/ai/) agent's workspace in a [Prized](https://prized.dev/sandboxes?via=pydantic-ai) sandbox, a Firecracker microVM.
Harness's `Coder`, `Shell`, `FileSystem` and every other workspace-aware capability run in it unchanged.

## Quickstart

Get an API key at https://prized.dev/onboarding/sandboxes?via=pydantic-ai (free credit, no card), then:

```sh
pip install pydantic-ai-prized "pydantic-ai-harness[coder,anthropic]"
export PRIZED_API_KEY=dcp_... ANTHROPIC_API_KEY=...
```

```python
from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder
from pydantic_ai_prized import PrizedSandbox

agent = Agent('anthropic:claude-sonnet-5-5', capabilities=[PrizedSandbox(), Coder()])
result = agent.run_sync('Clone https://github.com/un33k/python-slugify and run its tests.')
print(result.output)

# Continue in the same sandbox later; it keeps its files and installs.
agent.run_sync('Add a test for empty input.', workspace=result.workspace.ref)
```

Passing `message_history=result.all_messages()` continues in it too. Delete it with `await PrizedSandbox().destroy(result.workspace.ref)`, or let it pause after 10 idle minutes.

## How it behaves

- The run's first use creates a sandbox. A ref attaches to an existing one, and a paused one resumes on its next command.
- A run never deletes its sandbox. `PrizedSandbox().destroy(ref)` does, or the sandbox's own timers: `auto_pause_min` (10 by default), `auto_delete_min`, `max_runtime_min`.
- Commands run under `bash` as the user `prized`, starting in `/home/prized/work`, with stdin at EOF. A timeout or cancellation stops the command's whole process group.
- File operations go through the shell, so they reach any path the `prized` user can.
- A deleted or expired sandbox raises `WorkspaceUnavailableError`, also while a command runs in it. Pass `workspace='new'` to start over.
- `PrizedSandboxBackend` is the `WorkspaceBackend` itself, for use without the capability.

Options on `PrizedSandbox`: `template` (`prized-base-v1`, or `prized-agent-v1` with Claude Code and Codex), `vcpu`, `memory_mb`, `disk_gb`, `working_dir`, `env`, `labels`, the three timers, `api_key`, `base_url`.

## Limits

One region (us-west-2). Fixed Ubuntu 24.04 templates, no custom images or inbound ports. One command runs for up to an hour unless you pass a longer `timeout`, and keeps the first 4 MiB of each output stream (past that it raises `WorkspaceOutputLimitError`). A sandbox runs 24 hours by default, up to 7 days (each resume restarts the clock). On the free credit: 10 sandboxes at once, 2 vCPU and 4 GB each, 1 hour. Details: https://prized.dev/docs/sandboxes?via=pydantic-ai.

## Tests

```sh
pip install -e ".[test]"
PRIZED_API_KEY=dcp_... pytest -rs
```

Without a key only the offline tests run. With one:

- `tests/test_conformance.py` runs Pydantic AI's own `WorkspaceBackendSuite` against live sandboxes.
- `tests/test_agent_live.py` runs a real `Agent` with `Coder` (a scripted model, so no LLM key) and continues in the same sandbox by ref.

Both delete their sandboxes. `examples/fix_bug.py` has a real model fix a seeded bug in a sandbox.

## License

Apache-2.0.
