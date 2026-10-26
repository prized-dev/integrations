# Copyright 2026 Prized
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Live tests against the real Prized API.

They create real sandboxes (billed to the key's workspace) and delete every one.
Run with: PRIZED_API_KEY=dcp_... PRIZED_LIVE=1 pytest -m live -s
"""

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterable
from typing import Any

import prized
import pytest
from prized import AsyncSandbox
from strands import Agent
from strands.models import Model
from strands.sandbox import ExecutionResult, StreamChunk
from strands.sandbox.errors import SandboxPathNotFoundError, SandboxTimeoutError

from strands_prized import PrizedSandbox

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("PRIZED_API_KEY") and os.environ.get("PRIZED_LIVE")),
        reason="set PRIZED_API_KEY and PRIZED_LIVE=1 to run live tests",
    ),
]

RUN = uuid.uuid4().hex[:8]
LABELS = {"test": "strands-prized-live", "run": RUN}
# Small shapes: forks run 3 at once.
SMALL: dict[str, Any] = {"vcpu": 1, "memory_gb": 2}
SEEN: list[str] = []


def log(*parts: Any) -> None:
    print("[live]", *parts, flush=True)


async def state_of(sandbox_id: str) -> str:
    try:
        sb = await AsyncSandbox.connect(sandbox_id)
    except prized.PrizedError as e:
        if e.code == "not_found":
            return "gone"
        raise
    try:
        return sb.state
    finally:
        await sb.close()


GONE = ("deleting", "deleted", "gone")


@pytest.fixture(scope="module", autouse=True)
def _sweep():
    yield

    async def sweep() -> None:
        for sb in await AsyncSandbox.list(labels={"run": RUN}):
            log("sweeping leftover", sb.id, sb.state)
            await sb.delete(wait=True)
            await sb.close()

    asyncio.run(sweep())
    log("ids", " ".join(SEEN))


async def test_commands_code_and_files():
    t0 = time.monotonic()
    sandbox = PrizedSandbox(**SMALL, labels=LABELS)
    result = await sandbox.execute("pwd; python3 --version; echo oops >&2; exit 4")
    SEEN.append(sandbox.sandbox_id)
    log("first command incl. create", f"{time.monotonic() - t0:.2f}s", sandbox.sandbox_id)
    assert result.exit_code == 4
    assert result.stdout.startswith("/home/prized/work\nPython 3.12")
    assert result.stderr == "oops\n"

    assert (await sandbox.execute("echo $GREETING", env={"GREETING": "hey"})).stdout == "hey\n"
    await sandbox.execute("mkdir -p sub")
    assert (await sandbox.execute("pwd", cwd="sub")).stdout == "/home/prized/work/sub\n"

    py = await sandbox.execute_code("import sys; print(sys.version_info[:2])", "python3")
    assert py.stdout == "(3, 12)\n"
    js = await sandbox.execute_code("console.log(6 * 7)", "node")
    assert js.stdout == "42\n"
    left = await sandbox.execute("ls -A /home/prized/.prized/strands 2>/dev/null | wc -l")
    assert left.stdout.strip() == "0", "staged scripts are removed"

    await sandbox.write_file("data/blob.bin", bytes(range(256)))
    assert await sandbox.read_file("data/blob.bin") == bytes(range(256))
    await sandbox.write_text("/tmp/strands-live/x.txt", "outside home")
    assert await sandbox.read_text("/tmp/strands-live/x.txt") == "outside home"
    entries = {e.name: e for e in await sandbox.list_files("/home/prized/work")}
    assert entries["data"].is_dir is True and entries["sub"].is_dir is True
    assert {e.name: e.size for e in await sandbox.list_files("data")} == {"blob.bin": 256}
    await sandbox.remove_file("data/blob.bin")
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file("data/blob.bin")
    with pytest.raises(FileNotFoundError):
        await sandbox.remove_file("data/blob.bin")
    with pytest.raises(SandboxPathNotFoundError):
        await sandbox.list_files("nope")
    with pytest.raises(PermissionError):
        await sandbox.write_file("/root/x", b"no")

    with pytest.raises(SandboxTimeoutError) as info:
        await sandbox.execute("echo started; sleep 30", timeout=2)
    assert info.value.stdout == "started\n"

    sandbox_id = sandbox.sandbox_id
    await sandbox.aclose()
    assert await state_of(sandbox_id) in GONE


async def test_streaming_delivers_output_as_it_happens():
    async with PrizedSandbox(**SMALL, labels=LABELS) as sandbox:
        SEEN.append(sandbox.sandbox_id)
        t0 = time.monotonic()
        arrivals = []
        async for item in sandbox.execute_streaming("for i in 1 2 3; do echo $i; sleep 1; done; echo bye >&2"):
            arrivals.append((round(time.monotonic() - t0, 1), item))
        log("stream arrivals", [(t, getattr(i, "data", "result")) for t, i in arrivals])
        chunks = [i for _, i in arrivals if isinstance(i, StreamChunk)]
        assert "".join(c.data for c in chunks if c.stream_type == "stdout") == "1\n2\n3\n"
        assert [c.data for c in chunks if c.stream_type == "stderr"] == ["bye\n"]
        first_time = next(t for t, i in arrivals if isinstance(i, StreamChunk))
        assert first_time < 1.5, "the first line arrived before the command ended"
        final = arrivals[-1][1]
        assert isinstance(final, ExecutionResult) and final.exit_code == 0

        code = [c async for c in sandbox.execute_code_streaming("print('a'); print('b')", "python3")]
        assert code[-1].stdout == "a\nb\n"
    assert await state_of(sandbox.sandbox_id) in GONE


async def test_pause_then_attach_resumes_processes():
    sandbox = PrizedSandbox(**SMALL, labels=LABELS, on_close="pause")
    await sandbox.write_text("notes.md", "remember")
    SEEN.append(sandbox.sandbox_id)
    await sandbox.execute(
        "nohup bash -c 'i=0; while true; do i=$((i+1)); echo $i > /home/prized/count; sleep 0.2; done' "
        ">/dev/null 2>&1 &"
    )
    await asyncio.sleep(1)
    await sandbox.aclose()
    assert await state_of(sandbox.sandbox_id) in ("pausing", "paused")  # close doesn't wait

    again = PrizedSandbox(sandbox_id=sandbox.sandbox_id)
    assert await again.read_text("notes.md") == "remember"
    first = int(await again.read_text("/home/prized/count"))
    await asyncio.sleep(1)
    assert int(await again.read_text("/home/prized/count")) > first
    await again.aclose()  # attached: kept
    assert await state_of(sandbox.sandbox_id) == "running"
    sb = await AsyncSandbox.connect(sandbox.sandbox_id)
    await sb.delete(wait=True)
    await sb.close()


async def test_fork():
    async with PrizedSandbox(**SMALL, labels=LABELS) as parent:
        SEEN.append(parent.sandbox_id)
        await parent.write_text("state.txt", "parent")
        copies = await parent.fork(2)
        SEEN.extend(c.sandbox_id for c in copies)
        try:
            for i, copy in enumerate(copies):
                assert await copy.read_text("state.txt") == "parent"
                await copy.write_text("state.txt", f"copy {i}")
            assert await parent.read_text("state.txt") == "parent"
        finally:
            for copy in copies:
                await copy.aclose()
    for sandbox_id in [parent.sandbox_id] + [c.sandbox_id for c in copies]:
        assert await state_of(sandbox_id) in GONE


async def test_gone_sandbox_is_replaced():
    sandbox = PrizedSandbox(**SMALL, labels=LABELS)
    await sandbox.start()
    first = sandbox.sandbox_id
    SEEN.append(first)
    sb = await AsyncSandbox.connect(first)
    await sb.delete(wait=True)
    await sb.close()
    assert (await sandbox.execute("echo fresh")).stdout == "fresh\n"
    assert sandbox.sandbox_id != first
    SEEN.append(sandbox.sandbox_id)
    await sandbox.aclose()


class ScriptedModel(Model):
    """Plays back fixed model turns: no LLM and no model API key needed."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.turns = turns
        self.calls = 0

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def stream(
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ) -> AsyncIterable[Any]:  # noqa: E501
        turn = self.turns[self.calls]
        self.calls += 1
        yield {"messageStart": {"role": "assistant"}}
        if "tool" in turn:
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t{self.calls}", "name": turn["tool"]}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(turn["input"])}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": turn["text"]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}


def tool_results(agent: Agent) -> list[dict[str, Any]]:
    return [block["toolResult"] for message in agent.messages for block in message["content"] if "toolResult" in block]


def test_sync_agent_uses_the_sandbox_tools_across_calls():
    """agent(...) runs each call on a new event loop; the sandbox follows."""
    program = "for i in range(1, 16):\n    print('FizzBuzz' if i % 15 == 0 else i)\n"
    model = ScriptedModel(
        [
            {
                "tool": "sandbox_file_editor",
                "input": {"command": "create", "path": "/home/prized/work/fb.py", "file_text": program},
            },
            {"tool": "sandbox_shell", "input": {"command": "python3 fb.py | tail -1"}},
            {"text": "The last line is FizzBuzz."},
            {"tool": "sandbox_shell", "input": {"command": "ls fb.py && cat /proc/sys/kernel/hostname"}},
            {"text": "Still there."},
        ]
    )
    with PrizedSandbox(**SMALL, labels=LABELS) as sandbox:
        agent = Agent(model=model, sandbox=sandbox, callback_handler=None)
        reply = agent("Write fizzbuzz to fb.py and run it.")
        SEEN.append(sandbox.sandbox_id)
        assert "FizzBuzz" in str(reply)
        results = tool_results(agent)
        assert [r["status"] for r in results] == ["success", "success"], results
        assert "FizzBuzz" in json.dumps(results[1]["content"])
        first_id = sandbox.sandbox_id

        agent("Is fb.py still there?")
        results = tool_results(agent)
        assert results[-1]["status"] == "success", results[-1]
        assert "fb.py" in json.dumps(results[-1]["content"])
        assert sandbox.sandbox_id == first_id
    assert asyncio.run(state_of(first_id)) in GONE


def test_python_repl_tool_runs_in_the_sandbox():
    """Vended tools that read context.agent.sandbox (newer Strands releases) use it too."""
    python_repl = pytest.importorskip("strands.vended_tools.python_repl").python_repl
    with PrizedSandbox(**SMALL, labels=LABELS) as sandbox:
        agent = Agent(model=ScriptedModel([]), sandbox=sandbox, tools=[python_repl], callback_handler=None)
        out = agent.tool.python_repl(code="import os; print(os.getcwd(), os.environ['USER'])")
        SEEN.append(sandbox.sandbox_id)
        assert out["status"] == "success", out
        assert "/home/prized/work prized" in json.dumps(out["content"])
    assert asyncio.run(state_of(sandbox.sandbox_id)) in GONE


def test_gone_sandbox_is_replaced_between_sync_calls():
    """The common case: a sync agent's sandbox expires between two calls (new loops)."""
    sandbox = PrizedSandbox(**SMALL, labels=LABELS)
    asyncio.run(sandbox.execute("echo 1"))
    first = sandbox.sandbox_id
    SEEN.append(first)

    async def delete_behind_its_back() -> None:
        sb = await AsyncSandbox.connect(first)
        await sb.delete(wait=True)
        await sb.close()

    asyncio.run(delete_behind_its_back())
    result = asyncio.run(sandbox.execute("echo 2"))
    SEEN.append(sandbox.sandbox_id)
    assert result.stdout == "2\n" and "fresh, empty sandbox" in result.stderr
    assert sandbox.sandbox_id != first
    sandbox.close()
    assert asyncio.run(state_of(sandbox.sandbox_id)) in GONE


async def test_symlinks_modes_pip_and_imports():
    async with PrizedSandbox(**SMALL, labels=LABELS) as sandbox:
        SEEN.append(sandbox.sandbox_id)
        # A symlinked file (CLAUDE.md -> AGENTS.md is common) reads and writes through.
        await sandbox.execute("echo rules > AGENTS.md && ln -s AGENTS.md CLAUDE.md")
        assert await sandbox.read_text("CLAUDE.md") == "rules\n"
        await sandbox.write_text("CLAUDE.md", "new rules\n")
        assert (await sandbox.execute("cat AGENTS.md; test -L CLAUDE.md && echo link")).stdout == "new rules\nlink\n"
        # Editing a script keeps it executable.
        await sandbox.execute("printf 'echo v1\\n' > run.sh && chmod 755 run.sh")
        await sandbox.write_text("run.sh", "echo v2\n")
        assert (await sandbox.execute("./run.sh")).stdout == "v2\n"
        # pip install works without root, and its commands are on PATH.
        installed = await sandbox.execute("pip install -q pyfiglet && pyfiglet -f digital ok", timeout=180)
        assert installed.exit_code == 0, installed.stderr
        assert "|o|k|" in installed.stdout
        # Code run by execute_code imports modules from the working directory.
        await sandbox.write_text("helper.py", "VALUE = 42\n")
        assert (await sandbox.execute_code("import helper; print(helper.VALUE)", "python3")).stdout == "42\n"


async def test_close_from_inside_a_running_loop():
    """A notebook: a sync `with` block inside a running event loop still deletes."""
    with PrizedSandbox(**SMALL, labels=LABELS) as sandbox:
        await sandbox.execute("true")
        SEEN.append(sandbox.sandbox_id)
    assert await state_of(sandbox.sandbox_id) in GONE
