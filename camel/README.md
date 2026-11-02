# camel-prized

> **Not on PyPI yet.** Until it is, install `camel-prized` from this repo with the line below. The Prized Python SDK it uses (`prized`) is on PyPI.

Run a [CAMEL](https://github.com/camel-ai/camel) agent's code, shell commands and tools in a [Prized](https://prized.dev/?via=camel) sandbox: a Firecracker microVM with its own kernel that pauses with its memory and running processes.

## Install

```sh
pip install "camel-prized @ git+https://github.com/prized-dev/integrations#subdirectory=camel"
export PRIZED_API_KEY=dcp_...
```

Make the key at https://prized.dev/dashboard/workspace/api-keys?via=camel with **Full access**, or **Custom** with **Sandboxes: Write**. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=camel. Python 3.10 to 3.14, `camel-ai` 0.2.86 or later.

## Use it

Code execution tools for an agent (CAMEL's `CodeExecutionToolkit`, with a Prized sandbox in place of the local one):

```python
from camel.agents import ChatAgent
from camel_prized import PrizedCodeExecutionToolkit

with PrizedCodeExecutionToolkit() as toolkit:  # sandbox made on the first run, deleted at the end
    agent = ChatAgent("You solve problems by writing and running Python.", tools=toolkit.get_tools())
    print(agent.step("What is the 50th Fibonacci number? Run code to check.").msgs[0].content)
```

The interpreter on its own, or in any CAMEL class that takes a `BaseInterpreter` (such as `EmbodiedAgent(code_interpreter=...)`):

```python
from camel_prized import PrizedInterpreter

with PrizedInterpreter() as interpreter:
    interpreter.execute_command("pip install numpy")
    print(interpreter.run("import numpy; print(numpy.arange(6).reshape(2, 3))", "python"))
```

Tools that run in the sandbox, in the shape of CAMEL's `DaytonaRuntime`:

```python
from camel.agents import ChatAgent
from camel.toolkits import FunctionTool
from camel_prized import PrizedRuntime

def fetch_title(url: str) -> str:
    r"""Returns a web page's title.

    Args:
        url (str): The page.

    Returns:
        str: Its <title>.
    """
    import re, urllib.request
    html = urllib.request.urlopen(url, timeout=20).read().decode(errors="replace")
    return re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()

with PrizedRuntime() as runtime:
    runtime.add(FunctionTool(fetch_title)).build()
    agent = ChatAgent("You answer with the tools you have.", tools=runtime.get_tools())
```

## How it behaves

- **Lifecycle.** The first run creates the sandbox (`prized-base-v1`: Ubuntu 24.04, Python 3, Node.js 22, git), or `start()` / `build()` does it up front, and every later run reuses it. Closing (`close()`, `cleanup()`, or leaving the `with` block) deletes a sandbox it created. One left behind pauses itself after 10 idle minutes (`auto_pause_min`) and is deleted 60 minutes after that (`auto_delete_min`). With `sandbox_id=` it uses an existing sandbox and never deletes it.
- **Code.** `run(code, code_type)` takes `python` (also `py`, `py3`, `python3`), `javascript` (`js`, `node`) and `bash` (`sh`, `shell`). Each run is a new process as the user `prized` (no sudo) in `/home/prized/work`, with outbound internet: files and installed packages carry over between runs, variables don't. Large code goes up as a file first.
- **Output** comes back the way CAMEL's `SubprocessInterpreter` gives it: stdout, then `(stderr: ...)`, then a note if the code failed (`return code N`), ran past `timeout` (120 s by default; the command is stopped) or printed over 4 MiB.
- **Packages.** `execute_command("pip install ...")` works without root: every sandbox gets `PIP_BREAK_SYSTEM_PACKAGES=1` and `~/.local/bin` on its `PATH`.
- **Runtime tools** run the function's own source in the sandbox. It must be a plain function (not a method or lambda) that imports what it needs inside its body. Its return value comes back as JSON (anything else as a string); an exception comes back as `{"error": ..., "traceback": ...}`, as with `DaytonaRuntime`. What it prints is dropped.
- **Gone.** If a sandbox it created is gone (its run time ended, or it was deleted after pausing), the next run creates a fresh, empty one and says so at the top of that run's output.
- **More.** `interpreter.sandbox` (or `runtime.sandbox`) is the `prized.Sandbox` for anything else in the [Prized SDK](https://pypi.org/project/prized/): `files`, `pause()`, `fork()`.

Options for `PrizedInterpreter`, `PrizedRuntime` and `PrizedCodeExecutionToolkit`: `sandbox_id`, `template` (`prized-base-v1`, or `prized-agent-v1` with Claude Code and Codex), `vcpu`, `memory_gb`, `disk_gb`, `env_vars`, `labels`, `auto_pause_min`, `auto_delete_min`, `max_runtime_min`, `timeout`, `api_key`, `base_url`. The interpreter also takes `require_confirm` (off by default: the code runs in a remote microVM, not on your machine). Code in the sandbox, the model's included, can read `env_vars`: put there only what that code needs.

## Limits

On free credit, sandboxes run within the free tier: up to 10 at once, each up to 2 vCPU and 4 GB, with 60 minutes of running each. A top-up or a plan lifts that; a paid sandbox runs 24 hours by default, up to 7 days. No inbound ports, no custom images, no GPUs. Details: https://prized.dev/docs/sandboxes?via=camel

## Tests

```sh
pip install -e ".[dev]"
pytest                               # unit tests against a fake sandbox, no network
PRIZED_LIVE=1 pytest -m live -s      # live tests: real sandboxes, all deleted
```

License: Apache-2.0.
