# openhands-prized

Run the [OpenHands](https://github.com/OpenHands/OpenHands) Agent Server in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=openhands), which are Firecracker microVMs. This package gives you two things:

- **`PrizedWorkspace`**: a remote workspace for the [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk).
- **`openhands-prized serve`**: a sandbox as an [Agent Canvas](https://docs.openhands.dev/openhands/usage/agent-canvas/backends) backend.

You don't need an OpenHands fork, Docker or a public URL.

```bash
pip install openhands-sdk openhands-tools openhands-prized   # not on PyPI yet: until then, "openhands-prized @ git+https://github.com/prized-dev/integrations#subdirectory=openhands"
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=openhands
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=openhands.

## SDK: one workspace line

```python
import os

from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool
from openhands_prized import PrizedWorkspace

llm = LLM(model="anthropic/claude-sonnet-5-5", api_key=os.environ["ANTHROPIC_API_KEY"])
agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)])

with PrizedWorkspace() as workspace:  # instead of DockerWorkspace(...)
    conversation = Conversation(agent=agent, workspace=workspace)
    conversation.send_message("Write fizzbuzz.py with tests, and run them.")
    conversation.run()
    print(workspace.execute_command("ls; python3 -m pytest -q").stdout)
```

`PrizedWorkspace()` does the following:
1. Creates a sandbox (template `prized-base-v1`).
2. Installs `openhands-agent-server` at the same version as your `openhands-sdk`, with `uv`.
3. Starts the server.
4. Serves it to the SDK at `http://127.0.0.1:<port>`.

On exit the sandbox is deleted.

The agent loop, its tools and your LLM calls run in the sandbox. Your Prized key stays in your process.

## Agent Canvas: a sandbox as a backend

```bash
openhands-prized serve
```

It prints a **Host / Base URL** (`http://127.0.0.1:8780`, or another free port if that one is taken) and an **API key**. In Agent Canvas, open **Manage Backends → Add Backend** and enter both. Then set your LLM under Settings.

In Canvas, choose **Agent-server**, then **Local** (the proxy runs on your machine), and enter that URL and key. Canvas keeps LLM settings on each backend's server, so set your LLM under Settings once for the sandbox.

Ctrl-C pauses the sandbox. Its files, its conversations, its settings and the running server are kept. The next `openhands-prized serve` picks the same sandbox up in a few seconds, with the same URL and key, and Canvas reconnects by itself. Use `--new` for a fresh sandbox, `--sandbox <id>` for a specific one, `--fork <id>` for a copy of one (files, conversations, settings and the running server), or `--on-exit delete` to delete it on exit.

Tested with Agent Canvas `main` (October 30, 2026; Canvas 1.34.0, Agent Server 1.59.0) in Chromium. A coding task ran end to end from the Canvas UI. Then Ctrl-C, `serve` again, and a follow-up message in the same conversation. Canvas shows the backend as disconnected while the sandbox is paused and reconnects by itself, about 20 s after `serve` is back (its WebSocket retry backs off from 1 s up to 30 s). Conversations, the Files, Commits, Terminal and Usage panels, page reloads, and switching backends all work. Not on this backend yet:
- **Automations.** Canvas expects its automation service next to the Agent Server.
- **The browser tool**, since the sandbox has no Chromium.
- **VS Code.**

## How it connects

Your machine can't open a connection into a sandbox, so a proxy on `127.0.0.1` carries each request:

- **`exec`** (the default, on every plan): each HTTP request runs `curl` in the sandbox through Prized's command API. Each WebSocket (the SDK's event stream) is relayed by a small background script there. This adds about one command round trip per request and up to a second of event latency.
- **`ingress`** (`connection="ingress"`, or `"auto"` to use it when it answers): requests go straight to the sandbox's port URL, with the `Prized-Key` header added by the proxy. It needs sandbox port URLs, which are not turned on yet, so it has only been tested offline.

The Agent Server's own session key passes through the proxy unchanged. The proxy only answers to `127.0.0.1` and `localhost` Host headers. While the workspace is paused it answers 503, because anything reaching the sandbox would wake it.

## Options

| `PrizedWorkspace(...)` | Default | |
|---|---|---|
| `template`, `vcpu`, `memory_gb`, `disk_gb` | `prized-base-v1`, template shape | for a new sandbox |
| `labels`, `max_runtime_min`, `auto_pause_min` | `{"openhands": "workspace"}` | |
| `sandbox_id` | | attach to a sandbox instead (it is kept on exit, and a server already running there is reused) |
| `fork_from` | | fork a sandbox that already has the server installed (skips the install) |
| `on_exit` | `delete` (`keep` when attached) | or `pause`: memory, processes and files are kept |
| `connection` | `exec` | `ingress` or `auto` (see above) |
| `agent_server_version` | your `openhands-sdk` version | |
| `env`, `forward_env` | | variables for the server and commands (`forward_env` names local ones) |
| `extra_packages`, `server_args` | | more packages for the server's environment; more `agent-server` arguments |
| `working_dir` | `/home/prized/work` | |

`execute_command`, `file_upload` and `file_download` go straight to the sandbox through Prized. Everything else goes through the Agent Server. `pause()` and `resume()` pause the whole sandbox, server included. `workspace.sandbox` is the `prized.Sandbox`, and `workspace.timings` says where startup time went.

## Results

`scripts/live_e2e.py`, 2 vCPU / 4 GB, exec transport, OpenHands 1.59.0, October 30, 2026. The model was `claude-opus-5-5` through Prized's LLM gateway, which needed Claude Code's identity line added to the head of OpenHands' system prompt; nothing else changed. Five runs in a row passed, the last two on this exact code. Two earlier runs had failed on a `pause()` bug, which is now fixed. In every passing run the agent fixed two bugs so the unit tests pass, without editing the tests. In the last two runs, a 200 KB upload and a 3 MB download through the Agent Server also arrived intact.

| | Each run |
|---|---|
| Cold start (new sandbox to a ready workspace) | 17.1-17.9 s |
| The task (agent fixes the bugs and runs the tests) | 9.0-10.8 s |
| `pause()` + `resume()`, server still running | 0.8-2.0 s |
| Warm start with `fork_from` (the fix is there too) | 3.8-4.4 s |
| `serve`: Ctrl-C, then `serve` again (same sandbox, URL, key and server; files kept) | 4.1-4.4 s |
| Agent Canvas: a task sent from the UI, to the agent's last message (a Roman numerals module with tests, 79-85 passing) | 31-33 s |
| Agent Canvas: a follow-up in the same conversation after Ctrl-C and `serve` (94 tests passing) | 36 s |
| Agent Canvas: `serve` after Ctrl-C, sandbox resumed and server reused (`serve --fork`: 2.3-4.8 s) | 0.8-1.1 s |
| Sandbox cost at list price | $0.0014 |
| LLM cost (OpenHands' own count) | $0.03-0.09; in Canvas $0.14-0.17 per turn |

A request through the proxy takes 53 ms at the median (61 ms p90), from a client in the same AWS region. Socket output arrives about 0.4 s after the event.

## Limits

- **No Docker or root** in the sandbox. The agent runs as user `prized`, with `uv`, Python and Node available. Tools that need Docker don't work.
- **No browser tool:** the templates have no Chromium.
- **One Agent Server per sandbox and port.**
- **Cold start:** about 17 s. That is 0.1 s for the sandbox, 6 s for the install, and 11 s for the server's first start. A second start takes 5 s, so roughly half of the first start is compiling Python bytecode. `fork_from` a sandbox that already has the server takes about 4 s.
- **Run time:** a sandbox runs 24 hours by default on a paid plan, up to 7 days; 60 minutes on the free tier.

## Tests

```bash
scripts/test.sh openhands         # from the repo root: lint + offline tests
```

The offline tests use fake Prized sandboxes that run the package's in-sandbox scripts with your local bash, against a fake Agent Server. `scripts/live_e2e.py` runs a real agent end to end. It needs `PRIZED_API_KEY`, plus `LLM_MODEL` and `LLM_API_KEY` (and `LLM_BASE_URL` if you use one), and it deletes its sandbox at the end.

License: Apache-2.0.
