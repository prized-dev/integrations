"""Run a coding agent end to end in a live Prized sandbox and time each step.

    PRIZED_API_KEY=dcp_... uv run python scripts/live_agent.py [--model gpt-5.4] [--json out.json]

A `SandboxAgent` with the SDK's own shell and file tools (`exec_command`,
`write_stdin`, `apply_patch`) gets a small Python package whose tests fail. It
installs pytest into a venv (longer than the command's yield, so it polls the
running install with `write_stdin`), runs the tests, reads the code, patches
the bug, and runs the tests again. With
`--model` (and OPENAI_API_KEY) a real model drives it; without, scripted model
turns make the same tool calls a model would, so the run needs no model key.
The sandbox (2 vCPU, 4 GB, labelled) is deleted when the run ends.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from agents import RunConfig, Runner, set_tracing_disabled
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.entries import File
from agents.sandbox.session import CallbackSink, Instrumentation, SandboxSessionEvent
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call
from openai.types.responses import ResponseCustomToolCall

from openai_agents_prized import PrizedAPIError, PrizedSandboxClient, PrizedSandboxClientOptions

TASK = "The tests in this project fail. Find the bug, fix it, and show me the tests passing."

MANIFEST = Manifest(
    entries={
        "textstats/__init__.py": File(
            content=(
                b'"""Small text statistics."""\n\n\n'
                b"def word_count(text: str) -> int:\n"
                b'    """Count the words in `text`."""\n'
                b'    return len(text.split(" "))\n'
            )
        ),
        "tests/__init__.py": File(content=b""),
        "tests/test_textstats.py": File(
            content=(
                b"import unittest\n\n"
                b"from textstats import word_count\n\n\n"
                b"class WordCountTest(unittest.TestCase):\n"
                b"    def test_single_spaces(self):\n"
                b'        self.assertEqual(word_count("one two three"), 3)\n\n'
                b"    def test_runs_of_whitespace(self):\n"
                b'        self.assertEqual(word_count("  one\\ttwo\\n\\nthree  "), 3)\n'
            )
        ),
    }
)

PATCH = (
    "*** Begin Patch\n"
    "*** Update File: textstats/__init__.py\n"
    "@@\n"
    '-    return len(text.split(" "))\n'
    "+    return len(text.split())\n"
    "*** End Patch\n"
)


def _poll_install(call: ModelCall) -> list[Any]:
    """Poll the install the first turn left running, by the session id it got."""
    ids = re.findall(r"session ID (\d+)", json.dumps(call.input, default=str))
    if not ids:
        msg = "the install finished within its yield; nothing to poll"
        raise AssertionError(msg)
    args = {"session_id": int(ids[-1]), "chars": "", "yield_time_ms": 30000}
    return [function_call("write_stdin", args, call_id="c2")]


def scripted_model() -> ScriptedModel:
    """The tool calls a model makes for TASK, in order."""
    install = "python3 -m venv .venv && .venv/bin/pip install --quiet pytest && echo installed"
    return ScriptedModel(
        [
            [function_call("exec_command", {"cmd": install, "yield_time_ms": 1000}, call_id="c1")],
            ModelStep.respond(_poll_install),
            [
                function_call(
                    "exec_command", {"cmd": ".venv/bin/pytest -q 2>&1 | tail -n 5"}, call_id="c3"
                )
            ],
            [function_call("exec_command", {"cmd": "cat textstats/__init__.py"}, call_id="c4")],
            [
                ResponseCustomToolCall(
                    id="patch_item",
                    type="custom_tool_call",
                    name="apply_patch",
                    call_id="c5",
                    input=PATCH,
                )
            ],
            [
                function_call(
                    "exec_command", {"cmd": ".venv/bin/pytest -q 2>&1 | tail -n 3"}, call_id="c6"
                )
            ],
            [
                assistant_message(
                    "`word_count` split on single spaces, so runs of whitespace and tabs or "
                    "newlines counted wrong. It now uses `str.split()`; both tests pass."
                )
            ],
        ]
    )


async def main() -> int:  # noqa: C901, PLR0912, PLR0915  # one linear script
    """Run the agent, print the transcript and timings, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="a real model (needs OPENAI_API_KEY); default scripted")
    parser.add_argument("--json", type=Path, help="write the timings and transcript here")
    args = parser.parse_args()
    if not args.model:
        set_tracing_disabled(disabled=True)

    marks: dict[str, float] = {}
    started = time.monotonic()
    sandbox_ids: list[str] = []
    ops: list[dict[str, Any]] = []

    def record(event: SandboxSessionEvent, session: Any) -> None:  # noqa: ANN401
        sandbox_id = getattr(getattr(session, "state", None), "sandbox_id", None)
        if sandbox_id and sandbox_id not in sandbox_ids:
            sandbox_ids.append(sandbox_id)
        if event.phase == "finish":
            ops.append(
                {
                    "op": event.op,
                    "ok": getattr(event, "ok", None),
                    "ms": round(getattr(event, "duration_ms", 0.0), 1),
                }
            )
            marks.setdefault(f"first_{event.op}_done", time.monotonic() - started)

    client = PrizedSandboxClient(
        instrumentation=Instrumentation(sinks=[CallbackSink(record, mode="sync")])
    )
    options = PrizedSandboxClientOptions(
        vcpu=2,
        memory_mb=4096,
        max_runtime_min=15,
        labels={"test": "openai-agents-prized"},
    )
    agent = SandboxAgent(
        name="fixer",
        instructions="You are a careful engineer. Work in the sandbox with your tools.",
        model=args.model or scripted_model(),
        default_manifest=MANIFEST,
        capabilities=[Shell(), Filesystem()],
    )
    run_config = RunConfig(sandbox=SandboxRunConfig(client=client, options=options))
    try:
        result = await Runner.run(agent, TASK, run_config=run_config, max_turns=20)
    finally:
        await client.close()
    total = time.monotonic() - started

    transcript: list[dict[str, str]] = []
    for item in result.new_items:
        if isinstance(item, ToolCallItem):
            raw = item.raw_item
            name = getattr(raw, "name", None) or (raw.get("name") if isinstance(raw, dict) else "")
            arguments = getattr(raw, "arguments", None) or getattr(raw, "input", None) or ""
            transcript.append({"call": str(name), "input": str(arguments)})
        elif isinstance(item, ToolCallOutputItem):
            transcript.append({"output": str(item.output)})
    for entry in transcript:
        if "call" in entry:
            print(f"\n> {entry['call']}: {entry['input'].strip()}")
        else:
            print(entry["output"].rstrip())
    print(f"\nfinal answer: {result.final_output}")

    outputs = [e["output"] for e in transcript if "output" in e]
    polled = any("session ID" in o for o in outputs[:1]) and "installed" in "".join(outputs[:2])
    failed_first = any("1 failed" in o for o in outputs)
    passed_last = bool(outputs) and "2 passed" in outputs[-1]
    still_there = []
    probe = PrizedSandboxClient()
    try:
        for sandbox_id in sandbox_ids:
            # Only a 404 proves it gone; a lookup that fails says nothing, so
            # the sandbox counts as left running.
            try:
                sandbox = await probe._client.get_sandbox(sandbox_id)  # noqa: SLF001
            except PrizedAPIError as exc:
                if exc.code != "not_found":
                    still_there.append(sandbox_id)
                continue
            except Exception:  # noqa: BLE001
                still_there.append(sandbox_id)
                continue
            if sandbox.get("state") not in {"deleting", "deleted"}:
                still_there.append(sandbox_id)
    finally:
        await probe.close()

    summary = {
        "model": args.model or "scripted",
        "sandboxes": sandbox_ids,
        "left_running": still_there,
        "install_polled_with_write_stdin": polled,
        "tests_failed_first": failed_first,
        "tests_pass_after_fix": passed_last,
        "seconds_total": round(total, 2),
        "seconds_to_first_start": round(marks.get("first_start_done", -1), 2),
        "ops": ops,
        "transcript": transcript,
        "final_output": result.final_output,
    }
    print(
        f"\nsandbox {', '.join(sandbox_ids)}: start {summary['seconds_to_first_start']} s, "
        f"whole run {summary['seconds_total']} s, install polled: {polled}, "
        f"tests failed first: {failed_first}, "
        f"pass after the fix: {passed_last}, left running: {still_there or 'none'}"
    )
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2, default=str))
    return 0 if (polled and failed_first and passed_last and not still_there) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
