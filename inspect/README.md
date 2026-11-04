# inspect-prized

> **Not on PyPI yet.** Until it is, install `inspect-prized` from this repo with the line below. The Prized Python SDK it uses (`prized`) is on PyPI.

A sandbox environment for [Inspect AI](https://inspect.aisi.org.uk/) that runs each sample in its own [Prized](https://prized.dev/docs/sandboxes?via=inspect) sandbox, a Firecracker microVM in the cloud. Sample cleanup deletes it.

## Install

```sh
pip install "inspect-prized @ git+https://github.com/prized-dev/integrations#subdirectory=inspect"
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
| `keep` | `False` | Pause sandboxes after each sample instead of deleting them, to inspect them later. A sample forked from a `setup` template is deleted 7 days after it pauses. |
| `setup` | none | Shell commands that install what the task needs, in place of a Dockerfile. The first sample runs them in its own sandbox. For the second, they run once more in a sandbox that is then paused (the template), and that sample and every later one start as a fork of it with the install done. |
| `setup_timeout` | `1800` | Seconds each `setup` command may run. |
| `python_alias` | `True` | `python` runs `python3`, as in Python images (HumanEval, MBPP and other evals call `python`). It works through `PATH`, so if `env` sets `PATH`, put `/home/prized/.local/bin` first in it. |

```python
PrizedSandboxConfig(setup=["python3 -m venv ~/venv && ~/venv/bin/pip install numpy pandas"])
```

- How long a forked sample may run depends on the plan.
  - On a paid plan, each copy gets a fresh `max_runtime_min` from the fork.
  - On the free tier, forks never extend free time, so a copy gets what the template has left: `max_runtime_min` less what `setup` took. A paused template's clock is stopped, so that amount doesn't shrink while the eval runs.
- A sample runs `setup` in a fresh sandbox of its own in these cases:
  - its config has had no sample yet (with epochs or retries, the repeat is the second sample);
  - 4 other `setup` configs already have, or tried, a template;
  - its fork doesn't fit on the template's machine;
  - its config's template failed or was deleted to make room.

  So a setup that only one sample uses (a checkout per sample, say) never builds a template. A `setup` command that fails or times out fails the sample, as it would in the sample's own sandbox.
- The template stays paused until the eval ends, and is deleted then, even with `--no-sandbox-cleanup`. If the eval is killed before it can clean up, the template is deleted 7 days after it paused.

To branch a sample's state (try several continuations from one point of a trajectory), fork its sandbox: `copies = await sandbox().as_type(PrizedSandboxEnvironment).fork(3)`. Each copy is a live copy (files, memory, processes) and goes with the sample: deleted, or paused with `keep=True`.

Inspect runs up to 10 sandboxes at once by default (the free tier's limit). Raise it with `--max-sandboxes`.

A paused template counts toward the workspace's disk quota, and toward vCPU and memory when it keeps them for resume. It doesn't count toward the number of sandboxes. When a sample doesn't fit for that reason, templates are deleted one at a time until it fits or none is left. The samples of a config whose template was deleted run `setup` in their own sandboxes.

On the free tier (100 GB of disk), 10 samples of the default 10 GB disk fill it. Run with `--max-sandboxes 9` to keep one template. `fork()` copies count like any sandbox, and a `fork()` past a quota raises.

## Contract and limits

- Inspect's portable sandbox checks (`inspect_ai.util._sandbox.self_check`) pass against live sandboxes: 43 of 46 with inspect-ai 0.3.287. The other 3 add a user as root first, so they are expected to fail.
- Commands run as the user `prized` in `/home/prized/work`. There is no root or sudo, so `exec(user="root")` raises `SandboxUserUnsupportedError`.
- stdin (text, binary, 50 MiB), about 1 MiB of argv, cwd, env, timeouts (the command and its children are killed, then `TimeoutError`), signal exit codes (128+n), output up to Inspect's limit (10 MiB a stream by default, then `OutputLimitExceededError`), and Inspect's file errors (`FileNotFoundError`, `PermissionError`, `IsADirectoryError`, `OutputLimitExceededError`) all behave as with the Docker sandbox. Files can be written anywhere the user `prized` can write, `/tmp` and `/var/tmp` included.
- No Dockerfiles, compose files or custom images: use it for tasks that run on a stock Ubuntu machine. No inbound ports.
- Every sandbox of an eval carries that eval's `inspect-run` label (except samples kept with `keep=True`), and task cleanup deletes whatever is left with it, so a sample whose cleanup failed doesn't keep running. If the eval process is killed (SIGKILL, out of memory) nothing can clean up: each sample's sandbox is deleted when its `max_runtime_min` runs out, or delete them now with `inspect sandbox cleanup prized`.
- `inspect sandbox cleanup prized` deletes every sandbox labelled `created-by=inspect-prized`; `inspect sandbox cleanup prized <id>` deletes one, if this package made it.
- `connection()` gives `prized sandbox ssh <id>` for a shell into a running sample's sandbox (with the `prized` CLI).

## Tests

```sh
pip install -e '.[dev]'
PRIZED_LIVE=1 pytest tests        # runs Inspect's conformance checks against the real API
```

## License

Apache-2.0
