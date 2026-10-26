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

"""Unit tests for PrizedSandbox (no network: the Prized API is faked in memory)."""

import asyncio
import logging

import pytest
from fakes import FALLBACK_PATH, FakeSdk
from prized import CommandTimeoutError, ExecResult, PrizedError
from strands import Agent
from strands.sandbox import ExecutionResult, Sandbox, StreamChunk
from strands.sandbox.errors import SandboxPathNotFoundError, SandboxTimeoutError

from strands_prized import PrizedSandbox

SCRATCH = "/home/prized/.prized/strands/"


@pytest.fixture(name="sdk")
def _sdk():
    sdk = FakeSdk()
    with sdk.patch():
        yield sdk


def only(match, result):
    """A handler that answers commands containing ``match`` and leaves the rest alone."""

    def handler(cmd, kwargs):
        return result(cmd, kwargs) if match in str(cmd) else None

    return handler


def user_commands(box):
    return [(c, kw) for c, kw in box.calls if c != ["printenv", "PATH"]]


# ---- lifecycle ----------------------------------------------------------------------


async def test_is_a_strands_sandbox_and_starts_lazily(sdk):
    sandbox = PrizedSandbox()
    assert isinstance(sandbox, Sandbox)
    assert sdk.created == []
    await sandbox.execute("true")
    (box,) = sdk.created
    assert box.create_kwargs["template"] == "prized-base-v1"
    assert box.create_kwargs["labels"] == {"strands": "sandbox"}
    assert box.create_kwargs["env_vars"] == {"PIP_BREAK_SYSTEM_PACKAGES": "1"}
    assert box.create_kwargs["auto_pause_min"] == 10
    assert box.create_kwargs["auto_delete_min"] == 60
    assert box.create_kwargs["max_runtime_min"] is None
    assert sandbox.sandbox_id == box.id


async def test_create_options(sdk):
    sandbox = PrizedSandbox(
        template="prized-agent-v1", vcpu=4, memory_gb=8, env_vars={"A": "1"}, labels={"team": "x"}, on_close="pause"
    )
    await sandbox.start()
    kwargs = sdk.created[0].create_kwargs
    assert kwargs["template"] == "prized-agent-v1"
    assert (kwargs["vcpu"], kwargs["memory_gb"]) == (4, 8)
    assert kwargs["env_vars"] == {"PIP_BREAK_SYSTEM_PACKAGES": "1", "A": "1"}
    assert kwargs["labels"] == {"strands": "sandbox", "team": "x"}
    # A sandbox meant to be kept is not auto-deleted.
    assert kwargs["auto_delete_min"] is None


async def test_concurrent_first_use_creates_one_sandbox(sdk):
    sandbox = PrizedSandbox()
    await asyncio.gather(*(sandbox.execute("true") for _ in range(5)))
    assert len(sdk.created) == 1  # the fake's create yields, so a missing lock would show


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sandbox_id": "sbx_1", "template": "prized-agent-v1"},
        {"sandbox_id": "sbx_1", "vcpu": 2},
        {"sandbox_id": "sbx_1", "auto_delete_min": 5},
        {"sandbox_id": "sbx_1", "auto_pause_min": 5},
        {"sandbox_id": "sbx_1", "max_runtime_min": 5},
        {"on_close": "stop"},
        {"working_dir": "relative"},
    ],
)
def test_invalid_options_raise(kwargs):
    with pytest.raises(ValueError):
        PrizedSandbox(**kwargs)


async def test_aclose_deletes_a_created_sandbox_and_then_refuses_use(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    await sandbox.aclose()
    await sandbox.aclose()
    box = sdk.created[0]
    assert box.deleted
    assert sandbox.sandbox_id == box.id
    with pytest.raises(RuntimeError, match="closed"):
        await sandbox.execute("true")
    assert len(sdk.created) == 1


async def test_a_failed_close_can_be_retried(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    box = sdk.created[0]
    box.fail_delete = 1
    with pytest.raises(PrizedError):
        await sandbox.aclose()
    assert not box.deleted
    await sandbox.aclose()
    assert box.deleted


async def test_close_of_a_gone_sandbox_creates_nothing(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    sdk.created[0].state = "deleted"
    await asyncio.to_thread(sandbox.close)  # a new event loop: the reconnect finds it gone
    assert len(sdk.created) == 1


async def test_pause_on_close_then_attach(sdk):
    async with PrizedSandbox(on_close="pause") as first:
        await first.write_file("keep.txt", b"kept")
    box = sdk.created[0]
    assert box.state == "paused"
    async with PrizedSandbox(sandbox_id=first.sandbox_id) as again:
        assert await again.read_file("keep.txt") == b"kept"
    assert box.state == "paused" and not box.deleted  # attached: kept
    assert len(sdk.created) == 1


async def test_attach_to_a_missing_sandbox_raises(sdk):
    with pytest.raises(RuntimeError, match="gone"):
        await PrizedSandbox(sandbox_id="sbx_nope").start()


def test_sync_close_and_context_manager(sdk):
    with PrizedSandbox() as sandbox:
        asyncio.run(sandbox.execute("true"))
    assert sdk.created[0].deleted


async def test_sync_close_inside_a_running_loop_works(sdk):
    """A notebook or async app: `with PrizedSandbox()` still deletes on exit."""
    with PrizedSandbox() as sandbox:
        await sandbox.execute("true")
    assert sdk.created[0].deleted


async def test_fork_copies_are_deleted_on_close_by_default(sdk):
    parent = PrizedSandbox(on_close="keep", template="prized-agent-v1")
    await parent.write_file("state.txt", b"parent")
    copies = await parent.fork(2)
    assert len({c.sandbox_id for c in copies} | {parent.sandbox_id}) == 3
    for copy in copies:
        assert await copy.read_file("state.txt") == b"parent"
        assert copy._template == "prized-agent-v1"  # a replacement would match the parent
        await copy.aclose()
    assert all(k.deleted for k in sdk.created[0].kids)
    kept = await parent.fork(1, on_close="keep")
    await kept[0].aclose()
    assert not sdk.created[0].kids[-1].deleted


# ---- commands -------------------------------------------------------------------------


async def test_execute_maps_options_and_result(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    box = sdk.created[0]
    box.handler = only("make", lambda cmd, kw: ExecResult("out\n", "err\n", 2))

    result = await sandbox.execute("make", timeout=30, cwd="src", env={"K": "v"})

    assert result == ExecutionResult(exit_code=2, stdout="out\n", stderr="err\n")
    cmd, kwargs = user_commands(box)[-1]
    assert cmd == "make"
    assert kwargs["cwd"] == "/home/prized/work/src"
    assert kwargs["timeout"] == 30
    assert kwargs["on_stdout"] is None and kwargs["on_stderr"] is None  # one-request path
    assert kwargs["full_output"] is True  # its whole output, not the first 4 MiB of a plain call
    # pip-installed tools are on PATH, and pip may install without root.
    assert kwargs["env"] == {
        "PIP_BREAK_SYSTEM_PACKAGES": "1",
        "PATH": "/home/prized/.local/bin:" + FALLBACK_PATH,
        "K": "v",
    }
    await sandbox.execute("ls")
    cmd, kwargs = user_commands(box)[-1]
    assert kwargs["timeout"] == 0  # None: no limit
    assert kwargs["cwd"] == "/home/prized/work"


async def test_truncated_output_is_flagged(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    sdk.created[0].handler = only("yes", lambda cmd, kw: ExecResult("a" * 10, "", 0, truncated=True))
    result = await sandbox.execute("yes")
    assert "the output was cut off" in result.stderr


async def test_timeout_raises_strands_timeout_with_partial_output(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()

    def slow(cmd, kwargs):
        raise CommandTimeoutError(ExecResult("tick\n", "warn\n", -1, timed_out=True), kwargs["timeout"])

    sdk.created[0].handler = only("sleep", slow)
    with pytest.raises(SandboxTimeoutError) as info:
        await sandbox.execute("sleep 99", timeout=1)
    assert (info.value.stdout, info.value.stderr) == ("tick\n", "warn\n")


async def test_execute_streaming_yields_chunks_then_result(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()

    async def chatty(cmd, kwargs):
        if cmd != "count":
            return None
        kwargs["on_stdout"]("1\n")
        await asyncio.sleep(0)
        kwargs["on_stderr"]("oops\n")
        kwargs["on_stdout"]("2\n")
        return ExecResult("1\n2\n", "oops\n", 0)

    sdk.created[0].handler = chatty
    seen = [c async for c in sandbox.execute_streaming("count")]

    assert seen == [
        StreamChunk("1\n", "stdout"),
        StreamChunk("oops\n", "stderr"),
        StreamChunk("2\n", "stdout"),
        ExecutionResult(exit_code=0, stdout="1\n2\n", stderr="oops\n"),
    ]


async def test_stopping_a_stream_early_stops_the_command(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    box = sdk.created[0]

    async def forever(cmd, kwargs):
        if cmd != "tail -f log":
            return None
        kwargs["on_stdout"]("first\n")
        await asyncio.sleep(3600)

    box.handler = forever
    stream = sandbox.execute_streaming("tail -f log")
    assert await stream.__anext__() == StreamChunk("first\n", "stdout")
    await stream.aclose()
    assert box.cancelled_execs == 1


async def test_streaming_timeout_raises(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()

    def slow(cmd, kwargs):
        kwargs["on_stdout"]("x\n")
        raise CommandTimeoutError(ExecResult("x\n", "", -1, timed_out=True), 1)

    sdk.created[0].handler = only("sleep", slow)
    with pytest.raises(SandboxTimeoutError):
        _ = [c async for c in sandbox.execute_streaming("sleep 9", timeout=1)]


async def test_execute_code_feeds_the_script_on_stdin(sdk):
    sandbox = PrizedSandbox()
    result = await sandbox.execute_code("print('hi')", "python3")
    assert result.stdout == "ran python3: print('hi')"
    cmd, kwargs = user_commands(sdk.created[0])[-1]
    assert " python3 < " + SCRATCH in cmd  # imports resolve from the working directory
    assert kwargs["cwd"] == "/home/prized/work"
    assert not [p for p in sdk.created[0].fs if p.startswith(SCRATCH)]
    with pytest.raises(ValueError):
        await sandbox.execute_code("x", "python3; rm -rf /")


async def test_execute_code_streaming(sdk):
    sandbox = PrizedSandbox()
    chunks = [c async for c in sandbox.execute_code_streaming("1+1", "node")]
    assert chunks[-1].stdout == "ran node: 1+1"


# ---- files ----------------------------------------------------------------------------


async def test_files_inside_home(sdk):
    sandbox = PrizedSandbox()
    await sandbox.write_file("pkg/data.bin", b"\x00\x01")
    await sandbox.write_text("/home/prized/notes.md", "hello")
    box = sdk.created[0]
    assert box.fs["/home/prized/work/pkg/data.bin"] == b"\x00\x01"
    assert await sandbox.read_file("pkg/data.bin") == b"\x00\x01"
    assert await sandbox.read_text("/home/prized/notes.md") == "hello"
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file("missing.txt")
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file("/home/prized/work")  # a directory
    assert len(sdk.created) == 1  # nothing was replaced
    # Writes go through a staged file that the helper's trap removes.
    assert not [p for p in box.fs if p.startswith(SCRATCH)]


async def test_writes_keep_existing_files_in_place(sdk):
    """`cat >` into the target: no API re-upload, which would reset an executable to 0600."""
    sandbox = PrizedSandbox()
    await sandbox.write_file("run.sh", b"echo 1\n")
    box = sdk.created[0]
    writes = [c for c, _ in box.calls if isinstance(c, list) and c[:2] == ["bash", "-c"]]
    assert writes and writes[-1][-1] == "/home/prized/work/run.sh"
    assert 'cat -- "$1" > "$2"' in writes[-1][2]


async def test_symlinks_go_through_the_shell(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    box = sdk.created[0]
    box.fs["/home/prized/work/AGENTS.md"] = b"rules"
    box.symlinks["/home/prized/work/CLAUDE.md"] = "/home/prized/work/AGENTS.md"
    assert await sandbox.read_file("CLAUDE.md") == b"rules"
    await sandbox.write_file("CLAUDE.md", b"new rules")
    assert box.fs["/home/prized/work/AGENTS.md"] == b"new rules"


async def test_files_outside_home_go_through_the_shell(sdk):
    sandbox = PrizedSandbox()
    await sandbox.write_file("/tmp/x/out.txt", b"outside")
    box = sdk.created[0]
    assert box.fs["/tmp/x/out.txt"] == b"outside"
    assert await sandbox.read_file("/tmp/x/out.txt") == b"outside"
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file("/etc/missing")
    with pytest.raises(PermissionError):
        await sandbox.write_file("/root/secret", b"no")
    assert not [p for p in box.fs if p.startswith(SCRATCH)]


async def test_remove_file(sdk):
    sandbox = PrizedSandbox()
    await sandbox.write_file("a.txt", b"1")
    await sandbox.remove_file("a.txt")
    assert "/home/prized/work/a.txt" not in sdk.created[0].fs
    with pytest.raises(FileNotFoundError):
        await sandbox.remove_file("a.txt")


async def test_list_files(sdk):
    sandbox = PrizedSandbox()
    await sandbox.write_file("a.txt", b"123")
    sdk.created[0].dirs.add("/home/prized/work/sub")
    entries = await sandbox.list_files(".")
    assert [(e.name, e.is_dir, e.size) for e in entries] == [("a.txt", False, 3), ("sub", True, None)]
    with pytest.raises(SandboxPathNotFoundError):
        await sandbox.list_files("nope")
    with pytest.raises(SandboxPathNotFoundError):
        await sandbox.list_files("a.txt")
    assert len(sdk.created) == 1


# ---- a sandbox that is gone -----------------------------------------------------------


async def test_gone_sandbox_is_replaced_and_the_model_is_told(sdk, caplog):
    sandbox = PrizedSandbox()
    await sandbox.start()
    sdk.created[0].state = "deleted"
    with caplog.at_level(logging.WARNING):
        result = await sandbox.execute("echo hi")
    assert result.exit_code == 0
    assert len(sdk.created) == 2
    assert sandbox.sandbox_id == sdk.created[1].id
    assert "is gone" in caplog.text
    assert "fresh, empty sandbox" in result.stderr
    assert "fresh" not in (await sandbox.execute("echo again")).stderr  # told once


def test_gone_sandbox_is_replaced_on_a_new_event_loop(sdk):
    """Strands' sync agent(...) runs every call on a new loop, which reconnects first."""
    sandbox = PrizedSandbox()
    asyncio.run(sandbox.execute("echo 1"))
    sdk.created[0].state = "deleted"  # its run time ended between two agent calls
    result = asyncio.run(sandbox.execute("echo 2"))
    assert result.exit_code == 0
    assert len(sdk.created) == 2 and sandbox.sandbox_id == sdk.created[1].id
    asyncio.run(sandbox.aclose())
    assert sdk.created[1].deleted


async def test_parallel_calls_replace_a_gone_sandbox_once(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    sdk.created[0].state = "deleted"
    results = await asyncio.gather(*(sandbox.execute(f"echo {i}") for i in range(5)))
    assert all(r.exit_code == 0 for r in results)
    assert len(sdk.created) == 2


async def test_a_failed_replacement_is_retried_on_the_next_call(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    sdk.created[0].state = "deleted"
    sdk.fail_creates = 1
    with pytest.raises(PrizedError):
        await sandbox.execute("echo 1")
    assert (await sandbox.execute("echo 2")).exit_code == 0
    assert len(sdk.created) == 2


async def test_a_sandbox_in_error_is_deleted_when_replaced(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    failed = sdk.created[0]
    failed.state = "error"

    def refuse(cmd, kwargs):
        raise PrizedError("sandbox_not_running", status=409)

    failed.handler = only("echo", refuse)
    assert (await sandbox.execute("echo hi")).exit_code == 0
    assert failed.deleted and len(sdk.created) == 2


async def test_gone_attached_sandbox_is_not_replaced(sdk):
    owner = PrizedSandbox(on_close="keep")
    await owner.start()
    attached = PrizedSandbox(sandbox_id=owner.sandbox_id)
    await attached.start()
    sdk.created[0].state = "deleted"
    with pytest.raises(RuntimeError, match="gone"):
        await attached.execute("echo hi")
    assert len(sdk.created) == 1


async def test_other_errors_propagate(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()

    def unreachable(cmd, kwargs):
        raise PrizedError("sandbox_unreachable", status=502)

    sdk.created[0].handler = only("echo", unreachable)
    with pytest.raises(PrizedError, match="unreachable|did not answer"):
        await sandbox.execute("echo hi")
    assert len(sdk.created) == 1


# ---- event loops and Strands itself ----------------------------------------------------


def test_new_event_loops_reconnect(sdk):
    sandbox = PrizedSandbox()
    asyncio.run(sandbox.execute("echo 1"))
    asyncio.run(sandbox.execute("echo 2"))
    asyncio.run(sandbox.aclose())
    assert len(sdk.created) == 1
    assert len(sdk.connects) == 2
    assert sdk.created[0].deleted


def test_parallel_calls_on_a_new_loop_reconnect_once(sdk):
    sandbox = PrizedSandbox()
    asyncio.run(sandbox.start())

    async def burst():
        await asyncio.gather(*(sandbox.execute("true") for _ in range(5)))

    asyncio.run(burst())
    assert len(sdk.connects) == 1


def test_agent_registers_the_sandbox_tools_and_runs_them(sdk):
    sandbox = PrizedSandbox()
    agent = Agent(sandbox=sandbox, model="unused-model-id", callback_handler=None)
    assert {"sandbox_shell", "sandbox_file_editor"} <= set(agent.tool_names)
    # Direct tool calls go through Strands' sync bridge (a new event loop each).
    agent.tool.sandbox_file_editor(command="create", path="/home/prized/work/hi.py", file_text="print('hi')\n")
    box = sdk.created[0]
    assert box.fs["/home/prized/work/hi.py"] == b"print('hi')\n"
    box.handler = only("python3 hi.py", lambda cmd, kw: ExecResult("hi\n", "", 0))
    out = agent.tool.sandbox_shell(command="python3 hi.py")
    assert out["status"] == "success", out
    assert "hi" in str(out["content"])
    sandbox.close()
    assert box.deleted


# ---- threads, close races, and the remaining gone paths -------------------------------


def _threads(*targets):
    """Runs each target on its own thread (each with its own event loop)."""
    import threading

    errors = []

    def wrap(target):
        try:
            target()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=wrap, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_two_threads_starting_keep_one_sandbox(sdk):
    """Two agents (each sync, each its own loop) sharing one PrizedSandbox."""
    sdk.create_delay = 0.05
    sandbox = PrizedSandbox()
    errors = _threads(lambda: asyncio.run(sandbox.execute("a")), lambda: asyncio.run(sandbox.execute("b")))
    assert not errors, errors
    assert len(sdk.alive()) == 1  # a second create, if any, was deleted
    sandbox.close()
    assert not sdk.alive()


def test_two_threads_replace_a_gone_sandbox_once(sdk):
    sandbox = PrizedSandbox()
    asyncio.run(sandbox.start())
    sdk.created[0].state = "deleted"
    sdk.create_delay = 0.05
    results = []
    errors = _threads(
        lambda: results.append(asyncio.run(sandbox.execute("echo a"))),
        lambda: results.append(asyncio.run(sandbox.execute("echo b"))),
    )
    assert not errors, errors
    assert [r.exit_code for r in results] == [0, 0]
    assert len(sdk.alive()) == 1
    sandbox.close()
    assert not sdk.alive()


def test_threads_each_use_their_own_handle(sdk):
    sandbox = PrizedSandbox()
    asyncio.run(sandbox.start())

    def burst():
        async def go():
            await asyncio.gather(*(sandbox.execute("true") for _ in range(3)))

        asyncio.run(go())

    assert not _threads(burst, burst, burst)  # the fakes refuse cross-loop use
    assert len(sdk.created) == 1


async def test_a_call_in_flight_during_close_makes_no_new_sandbox(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    box = sdk.created[0]
    started = asyncio.Event()

    async def slow(cmd, kwargs):
        if cmd != "long":
            return None
        started.set()
        await asyncio.sleep(0.05)
        box.check_alive()  # deleted meanwhile: not_found

    box.handler = slow
    call = asyncio.ensure_future(sandbox.execute("long"))
    await started.wait()
    await sandbox.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await call
    assert len(sdk.created) == 1 and box.deleted


def test_close_on_a_new_loop_deletes_a_failed_sandbox(sdk):
    sandbox = PrizedSandbox()
    asyncio.run(sandbox.start())
    sdk.created[0].state = "error"
    sandbox.close()
    assert sdk.created[0].deleted and len(sdk.created) == 1


async def test_close_pauses_without_waiting(sdk):
    sandbox = PrizedSandbox(on_close="pause")
    await sandbox.start()
    await sandbox.aclose()
    assert sdk.created[0].pause_kwargs == [{"wait": False}]


async def test_path_is_read_once_per_sandbox(sdk):
    sandbox = PrizedSandbox()
    for _ in range(3):
        await sandbox.execute("true")
    assert len([c for c, _ in sdk.created[0].calls if c == ["printenv", "PATH"]]) == 1


async def test_permission_denied_reads_go_through_the_shell(sdk):
    sandbox = PrizedSandbox()
    await sandbox.start()
    box = sdk.created[0]
    box.fs["/home/prized/work/locked.txt"] = b"x"

    async def refuse(path):
        raise PrizedError("permission_denied", status=403)

    box.handles[0].files.read_bytes = refuse
    assert await sandbox.read_file("locked.txt") == b"x"
    assert box.calls[-1][0] == ["base64", "-w0", "--", "/home/prized/work/locked.txt"]


async def test_fork_copies_inherit_the_create_options(sdk):
    parent = PrizedSandbox(vcpu=1, memory_gb=2, labels={"team": "x"})
    (copy,) = await parent.fork(1)
    assert copy._create_kwargs == parent._create_kwargs
    await copy.aclose()
