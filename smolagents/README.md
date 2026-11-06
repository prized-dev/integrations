# smolagents-prized

> **Not on PyPI yet.** Until it is, install `smolagents-prized` from this repo with the line below. The Prized Python SDK it uses (`prized`) is on PyPI.

Run a [smolagents](https://github.com/huggingface/smolagents) `CodeAgent`'s Python in a [Prized](https://prized.dev/?via=smolagents) sandbox: a Firecracker microVM with its own kernel that pauses with its memory and running processes. The agent's code, the packages it installs and the variables it builds up all live in the sandbox, not on your machine — and because the sandbox pauses *with its memory*, that state is still there when the agent takes its next turn.

It's a drop-in `PythonExecutor`, so nothing about your agent changes except where the code runs.

## Install

```sh
pip install "smolagents-prized @ git+https://github.com/prized-dev/integrations#subdirectory=smolagents"
export PRIZED_API_KEY=dcp_...
```

Make the key at https://prized.dev/dashboard/workspace/api-keys?via=smolagents with **Full access**, or **Custom** with **Sandboxes: Write**. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=smolagents. Python 3.10 to 3.14, `smolagents` 1.21 or later.

## Use it

Pass a `PrizedExecutor()` as the agent's `executor`:

```python
from smolagents import CodeAgent, InferenceClientModel
from smolagents_prized import PrizedExecutor

agent = CodeAgent(
    tools=[],
    model=InferenceClientModel(),
    executor=PrizedExecutor(),            # a sandbox is created here and the kernel starts
    additional_authorized_imports=["*"],
)
try:
    print(agent.run("What is the 50th Fibonacci number? Compute it."))
finally:
    agent.cleanup()                       # stops the kernel and deletes the sandbox
```

Install packages the agent will need, attach to an existing sandbox, or size it:

```python
PrizedExecutor(additional_imports=["pandas", "matplotlib"])   # pip-installed in the sandbox up front
PrizedExecutor(sandbox_id="sbx_...")                          # attach; cleanup() won't delete it
PrizedExecutor(vcpu=4, memory_gb=8, template="prized-base-v1")
```

## How it works

- `PrizedExecutor()` creates a Prized sandbox (or attaches to `sandbox_id`) and starts one long-lived Python process — the *kernel* — inside it.
- The kernel holds a single namespace in memory. smolagents sends the tools and variables once; every later step runs in that same namespace, so imports, variables and tool definitions persist across steps.
- Each step is one code cell: the executor uploads the code and the kernel runs it, returning the printed logs, the value of the last expression, and whether the agent called `final_answer(...)`. `!pip install ...` lines run as shell commands, as in a notebook.
- The kernel listens on a unix socket inside the sandbox — **no network port is opened**.
- The sandbox pauses after `auto_pause_min` idle minutes (keeping the kernel and its namespace) and is deleted after `auto_delete_min`, so a forgotten executor can't run up a bill. `cleanup()` (called by `CodeAgent.cleanup()`) stops the kernel and deletes a sandbox it created.

## Options

`PrizedExecutor(additional_imports=None, logger=None, allow_pickle=False, *, api_key=None, base_url=None, template="prized-base-v1", sandbox_id=None, vcpu=None, memory_gb=None, disk_gb=None, env_vars=None, labels=None, auto_pause_min=10, auto_delete_min=60, max_runtime_min=None, step_timeout=300.0, startup_timeout=90.0)`

- `additional_imports` — pip packages installed in the sandbox before the run.
- `allow_pickle` — as in smolagents' remote executors: JSON-only serialization by default; set `True` only if you trust the environment and need pickle for custom types.
- `sandbox_id` — attach to an existing sandbox instead of creating one (then `cleanup()` leaves it alone).
- `step_timeout` / `startup_timeout` — seconds for one code cell, and for the kernel to come up.

## Limits

- Rich display data (images, charts, HTML) from a cell is not captured; printed output and the last expression's value are. `final_answer(...)` returns real Python objects (JSON-safe by default; see `allow_pickle`).
- One kernel runs cells one at a time, which is how a smolagents agent drives it.

## Tests

```sh
scripts/test.sh smolagents            # lint + offline tests, no key, no network
FLOOR=1 scripts/test.sh smolagents    # against the oldest smolagents the package supports
```

The offline tests run a real kernel on a real unix socket inside a stand-in sandbox (bash in a temp directory), so the whole protocol is exercised without touching the Prized API.

Live tests create a real sandbox, billed to your workspace, and delete it at the end:

```sh
pip install -e ".[dev]"
PRIZED_LIVE=1 PRIZED_API_KEY=dcp_... pytest -m live
```
