# Compatibility

The latest run of this package against the real Agents SDK and the real Prized API. Live runs create real sandboxes; the offline tests run on every change in CI.

## 2026-10-31

What ran: `openai-agents-prized` 0.1.0 as published on PyPI (wheel sha256 `0a2f10e734fbdc46bf26864d71922db34f50a1954a51d76ae71154e55b1f4b77`), whose `openai_agents_prized/` files are identical to this folder's at commit `0b64bfa`, with this folder's `tests/` and `scripts/`. Ubuntu 24.04, packages installed with uv 0.13.6.

| `openai-agents` | Python | What ran | Result |
| --- | --- | --- | --- |
| 0.24.0 (latest release), httpx 0.28.1 | 3.13.18 | All 153 tests: 150 offline and 3 live | 153 passed |
| 0.23.1 (oldest allowed), httpx 0.27.0 | 3.10.22 | The 3 live tests | 3 passed |
| `main` at `e355d69` (2026-10-30, unreleased) | 3.13 | All 153 tests | 153 passed |
| 0.24.0 | 3.13.18 | `scripts/live_agent.py`, scripted model turns | Passed |

The live tests:

- `test_live_lifecycle[new-sandbox]` is the SDK's own pause-and-resume scenario (`tests/sandbox/integration_tests/test_runner_pause_resume.py`), ported to Prized without mounts. A `SandboxAgent` with scripted turns materializes a manifest with every other entry type, including a real clone of `octocat/Hello-World`, writes and patches files, is refused outside the workspace, uses a TTY, and stops on a tool that needs approval. The run state goes through JSON, and the resumed run, in a new sandbox restored from the snapshot, checks that every file came back.
- `test_live_lifecycle[pause-on-exit]` is the same scenario with `pause_on_exit=True`: the resumed run continues in the same, paused sandbox.
- `test_live_processes` drives the PTY calls behind `exec_command` and `write_stdin` on a real sandbox: output in parts, a TTY with Ctrl-C, and `pty_terminate_all()`, which the SDK's stop and shutdown call, ending every process.

`scripts/live_agent.py` gives a `SandboxAgent` a small Python project whose tests fail. Its turns make the tool calls a model would: install pytest (polled with `write_stdin`), run the tests (one fails), read the code, fix it with `apply_patch`, run the tests again (both pass). The sandbox was deleted at the end, and no sandbox labelled `test=openai-agents-prized` was left in the workspace.

## Run it yourself

```bash
cd openai-agents
PRIZED_LIVE_TESTS=1 PRIZED_API_KEY=dcp_... uv run pytest              # offline and live tests
PRIZED_API_KEY=dcp_... uv run python scripts/live_agent.py             # the coding agent, scripted turns
```

The live tests create four 2 vCPU sandboxes and delete them.
