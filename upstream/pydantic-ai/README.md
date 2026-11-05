# PrizedSandbox for Pydantic AI Harness (upstream contribution)

`PrizedSandbox`, a Pydantic AI Harness capability that runs an agent's workspace (`Coder`, `Shell`, `FileSystem` and the rest) in a [Prized](https://prized.dev/?via=pydantic-ai) sandbox, written to go into [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) next to the Modal, E2B, Daytona and boxd sandboxes. It is not a package of its own: it imports the harness's internal helpers, so it only runs inside a pydantic-ai checkout. Until it lands upstream, [`pydantic-ai-prized`](../../pydantic-ai) is the installable version.

- `files/` holds the new files at their paths in pydantic-ai: the capability and backend (`src/pydantic_ai_harness/pydantic_ai_harness/prized_sandbox/`), the docs page, and the tests (a fake Prized SDK, the core conformance suite over the fake and live, unit tests, and a live tier).
- `apply.py` copies them into a checkout and makes the shared edits (the `prized` extra and its dated cooldown entry, the `prized_live` marker, the lazy exports, the docs tables, navigation and provider lists, the threat model, the harness skill, and the registry entries the repo's own tests check), next to the matching boxd lines.
- `prized-sandbox.patch` is the same change as one `git diff` against pydantic-ai `8b455f22` (28 files), for `git apply` on that commit.
- `agent_demo.py` runs a real agent: it writes and runs code, pauses with a server running, resumes from the history, and forks live.

## Apply and test

```sh
git clone https://github.com/pydantic/pydantic-ai && cd pydantic-ai
python3 /path/to/upstream/pydantic-ai/apply.py .
uv lock                                    # adds prized to uv.lock
uv sync --frozen --all-packages --group lint --extra prized
uv run pytest tests/harness/prized_sandbox # offline: fake SDK, no key
PYDANTIC_AI_HARNESS_PRIZED_LIVE=1 PRIZED_API_KEY=dcp_... uv run pytest -m prized_live tests/harness/prized_sandbox
```

Made against pydantic-ai `8b455f22` (2026-11-05). Results on that commit with prized 0.1.2:

- Offline: 63 tests pass, the whole conformance suite over the fake among them (37 rules pass; the `SupportsRealpath` rule skips, as this backend leaves realpath to the shell), plus the repo's registry tests (`test_docs_parity`, `test_capability_combine`, `test_history_append_only`: 751 in all). `ruff format`, `ruff check` and `pyright` are clean on the new files.
- Live, against Prized's production API: 44 pass, with the same rule skipped. That is the conformance suite over a real sandbox, plus process-group stops, pause with a running process, live fork with a running process, deletion mid-command, and `Coder`'s tools. The final run took 553 s.
- The live run was made from a sandbox that held no Prized key; its API calls went through a relay that added the key outside it. That relay adds about 0.3 s per call, so the conformance suite's 8 MiB file round trip took 282 s there: its write is about 170 shell calls, while the read is one file API call. Direct, the write is a fraction of that.
- A real agent (`agent_demo.py`, Claude Sonnet 5.5):
  - It wrote and ran a prime sieve (9592) and started an HTTP server.
  - The sandbox paused in 0.46 s. The next run, from the message history, resumed it with the server still answering (200).
  - A fork took 1.61 s, and the copy had the file and the running server.

## What it does

- A run with no ref creates a sandbox on first use (`prized-base-v1` by default), labelled `created-by=pydantic-ai`; a later run reattaches from the message history or a `WorkspaceRef`. A deleted sandbox raises `WorkspaceUnavailableError`, never a silent fresh one.
- New sandboxes pause after 10 idle minutes (`auto_pause_min`) with their memory and processes; the next command resumes them.
- `PrizedSandbox().pause(ref)`, `.fork(ref)` (a live copy: files, memory, running processes) and `.destroy(ref)`.
- Reads of files under `/home/prized` use the SDK's file API (one call; 8 MiB in 0.35 s live); writes and the rest go through core's shell filesystem, since the file API replaces files whole and so would write a file whose mode forbids it.
- Commands with a `timeout` up to 240 s are one exec call; longer or unbounded ones run detached and are polled. A timeout or cancellation stops the command's process group. Long command lines (the shell-derived file writes) go through a file written with the file API, since the edge refuses a wrapped command line past about 64 KiB.
