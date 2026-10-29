# inspect-prized

> **Coming 2026-11-05.** `inspect-prized` is not on PyPI yet. The Prized Python SDK it uses (`prized`) is.

A sandbox environment for [Inspect AI](https://inspect.aisi.org.uk/) that runs each sample in its own [Prized](https://prized.dev/docs/sandboxes?via=inspect) sandbox, a Firecracker microVM in the cloud. Sample cleanup deletes it.

## Install

```sh
pip install inspect-prized
export PRIZED_API_KEY=dcp_...   # prized.dev/dashboard/workspace/api-keys?via=inspect
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=inspect.

The package registers `prized` through Inspect's entry point, so no import is needed for `--sandbox prized` or `sandbox="prized"`.

## Use

```python
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import bash
from inspect_prized import PrizedSandboxConfig


@task
def arithmetic() -> Task:
    return Task(
        dataset=[Sample(input="Use Python in bash to calculate 6 * 7.", target="42")],
        solver=[use_tools(bash()), generate()],
        scorer=includes(),
        sandbox=("prized", PrizedSandboxConfig(vcpu=2, memory_gb=4)),
    )
```

```sh
inspect eval arithmetic.py --model openai/gpt-5
```

`examples/fix_bug.py` is a fuller task: the agent fixes a bug with the bash tool and the scorer runs the tests in the same sandbox.

## Configuration

| Field | Default | Meaning |
|---|---|---|
| `template` | `prized-base-v1` | `prized-base-v1` (Ubuntu 24.04 with Python, Node and git) or `prized-agent-v1` (adds Claude Code and Codex). |
| `vcpu`, `memory_gb`, `disk_gb` | template's | Shape of each sample's sandbox. |
| `env` | none | Variables for every command. |
| `labels` | none | Extra labels; every sandbox also carries `created-by=inspect-prized`. |
| `max_runtime_min` | `60` | Prized deletes the sandbox after this many running minutes, even if cleanup never ran. 60 is the free tier's maximum; on a paid plan, raise it for samples that run longer. |
| `keep` | `False` | Pause sandboxes after each sample instead of deleting them, to inspect them later. |
| `setup` | none | Shell commands that install what the task needs, in place of a Dockerfile. They run once, before the first sample, in a sandbox that is then paused, and every sample starts as a fork of it with the install done. |
| `setup_timeout` | `1800` | Seconds each `setup` command may run. |
| `python_alias` | `True` | `python` runs `python3`, as in Python images (HumanEval, MBPP and other evals call `python`). |

```python
PrizedSandboxConfig(setup=["python3 -m venv ~/venv && ~/venv/bin/pip install numpy pandas"])
```

If a fork doesn't fit on the template's machine, that sample gets a fresh sandbox and runs `setup` itself. On the free tier a fork keeps its template's remaining run time (60 minutes of running in all, less what `setup` took); a paid plan gives each fork its own `max_runtime_min`.

To branch a sample's state (try several continuations from one point of a trajectory), fork its sandbox: `copies = await sandbox().as_type(PrizedSandboxEnvironment).fork(3)`. Each copy is a live copy (files, memory, processes) and is deleted with the sample.

Inspect runs up to 10 sandboxes at once by default (the free tier's limit). Raise it with `--max-sandboxes`.

## Contract and limits

- Inspect's portable sandbox checks (`inspect_ai.util._sandbox.self_check`) pass against live sandboxes: 43 of 44, with the root-user check expected to fail.
- Commands run as the user `prized` in `/home/prized/work`. There is no root or sudo, so `exec(user="root")` raises `SandboxUserUnsupportedError`.
- stdin (text, binary, 50 MiB), about 1 MiB of argv, cwd, env, timeouts (the command and its children are killed, then `TimeoutError`), signal exit codes (128+n), output up to Inspect's limit (10 MiB a stream by default, then `OutputLimitExceededError`), and Inspect's file errors (`FileNotFoundError`, `PermissionError`, `IsADirectoryError`, `OutputLimitExceededError`) all behave as with the Docker sandbox. Files can be written anywhere the user `prized` can write, `/tmp` and `/var/tmp` included.
- No Dockerfiles, compose files or custom images: use it for tasks that run on a stock Ubuntu machine. No inbound ports.
- Every sandbox of an eval carries that eval's `inspect-run` label, and task cleanup deletes whatever is left with it, so a sample whose cleanup failed doesn't keep running.
- `inspect sandbox cleanup prized` deletes every sandbox labelled `created-by=inspect-prized`; `inspect sandbox cleanup prized <id>` deletes one, if this package made it.
- `connection()` gives `prized sandbox ssh <id>` for a shell into a running sample's sandbox (with the `prized` CLI).

## Tests

```sh
pip install -e '.[dev]'
PRIZED_LIVE=1 pytest tests        # runs Inspect's conformance checks against the real API
```

## License

Apache-2.0
