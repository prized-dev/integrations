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

"""Regression tests for PrizedShellTool: each failed before its fix."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from prized import ExecResult

from agent_framework_prized import PrizedShellTool
from agent_framework_prized import _shell as shell_module
from tests.fakes import FakeSdk, cut_result, output_read_error

STATE = "/home/prized/.prized/maf/shell-state.sh"


@pytest.fixture
def sdk():
    fake = FakeSdk()
    with fake.patch():
        yield fake


# a command the SDK runs detached is quoted twice into one argument.


async def test_a_70k_command_streamed_runs(sdk):
    shell = PrizedShellTool(on_output=lambda s, t: None)
    result = await shell.run(f"printf '%s' '{'x' * 70_000}' | wc -c")
    assert result.stdout.strip() == "70000", result.stderr
    await shell.close()


async def test_a_45k_quote_heavy_heredoc_with_no_timeout_runs(sdk):
    shell = PrizedShellTool(timeout=None)
    code = "print('hello')\n" * 3000
    result = await shell.run(f"cat > m.py <<'EOF'\n{code}EOF\nwc -l < m.py")
    assert result.stdout.strip() == "3000", result.stderr
    await shell.close()


# env= stays in charge, and is never written to disk.


async def test_new_env_on_reattach_wins_over_the_saved_session(sdk):
    first = PrizedShellTool(env={"TOKEN": "old-secret", "ONLY_FIRST": "1"}, on_close="keep")
    await first.run("cd /home/prized && export MINE=kept")
    remote = sdk.created[0]
    await first.close()
    second = PrizedShellTool(sandbox_id=remote.id, env={"TOKEN": "rotated"})
    result = await second.run('pwd; echo "$TOKEN ${ONLY_FIRST-unset} $MINE"')
    assert result.stdout == "/home/prized\nrotated unset kept\n"
    await second.close()
    remote.state = "deleted"


async def test_env_secrets_are_not_written_to_the_state_file(sdk):
    shell = PrizedShellTool(env={"GITHUB_TOKEN": "ghp_supersecret"})
    await shell.run("cd /home/prized && export OTHER=1")
    assert "ghp_supersecret" not in sdk.created[0].read(STATE)
    await shell.close()


async def test_a_variable_a_command_changes_or_unsets_is_kept(sdk):
    shell = PrizedShellTool(env={"A": "from-env", "B": "from-env"})
    await shell.run("export A=changed; unset B")
    assert (await shell.run('echo "$A ${B-unset}"')).stdout == "changed unset\n"
    await shell.close()


# removing the working directory doesn't break the tool.


async def test_tool_survives_the_model_deleting_the_working_dir(sdk):
    shell = PrizedShellTool(on_close="keep")
    assert (await shell.run("cd /home/prized && rm -rf work && echo gone")).stdout == "gone\n"
    assert (await shell.run("echo still-here; pwd")).stdout == "still-here\n/home/prized\n"
    await shell.run("cd work 2>/dev/null || echo no-work-yet")
    await shell.close()
    assert (await shell.run("cd /home/prized/work && pwd")).stdout == "/home/prized/work\n"
    sdk.created[0].state = "deleted"


async def test_stateless_tool_remakes_its_working_dir(sdk):
    shell = PrizedShellTool(mode="stateless")
    await shell.run("rm -rf /home/prized/work")
    assert (await shell.run("pwd")).stdout == "/home/prized/work\n"
    await shell.close()


# fork() checks on_close before making copies.


async def test_fork_with_bad_on_close_makes_no_copy(sdk):
    shell = PrizedShellTool()
    await shell.start()
    with pytest.raises(ValueError):
        await shell.fork(2, on_close="destroy")  # type: ignore[arg-type]
    await shell.close()
    assert sdk.alive() == []


# a command's own EXIT trap doesn't lose its session state.


async def test_user_exit_trap_keeps_session_state(sdk):
    shell = PrizedShellTool()
    await shell.run("t=$(mktemp -d); trap 'rm -rf \"$t\"' EXIT; cd /home/prized; export STAGE=2")
    assert (await shell.run('pwd; echo "$STAGE"')).stdout == "/home/prized\n2\n"
    await shell.run("echo trailing \\")  # a trailing backslash doesn't swallow the save
    assert (await shell.run("pwd")).stdout == "/home/prized\n"
    await shell.close()


# shell options are kept, so a function that needs one survives.


async def test_extglob_function_does_not_wipe_later_functions(sdk):
    shell = PrizedShellTool()
    await shell.run("shopt -s extglob\nclean() { rm -f !(keep.txt); }\nzz() { echo zz-ok; }")
    assert (await shell.run("zz")).stdout == "zz-ok\n"
    assert (await shell.run("shopt -q extglob && echo on")).stdout == "on\n"
    await shell.close()


# handles on other live event loops are closed on close and on replacement.


def test_close_releases_handles_of_every_live_loop(sdk):
    shell = PrizedShellTool(mode="stateless")
    other = asyncio.new_event_loop()
    t = threading.Thread(target=other.run_forever, daemon=True)
    t.start()
    try:
        asyncio.run_coroutine_threadsafe(shell.run("true"), other).result(10)
        asyncio.run(shell.close())
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), other).result(5)  # let it close
        remote = sdk.created[0]
        assert [h for h in remote.handles if not h.closed and not h.loop.is_closed()] == []
    finally:
        other.call_soon_threadsafe(other.stop)
        t.join(5)
        other.close()


async def test_a_replacement_closes_the_old_handle(sdk):
    shell = PrizedShellTool()
    await shell.run("true")
    first = sdk.created[0]
    first.state = "deleted"
    await shell.run("true")
    await asyncio.sleep(0.01)
    assert all(h.closed for h in first.handles)
    await shell.close()


# each command keeps its whole output (up to the SDK's 100 MiB a stream), not the
# first 4 MiB one plain call keeps; past that, the model is told.


async def test_commands_keep_their_whole_output(sdk):
    for mode in ("persistent", "stateless"):
        shell = PrizedShellTool(mode=mode)
        assert (await shell.run("echo hi")).stdout == "hi\n"
        await shell.close()
    assert len(sdk.created) == 2
    for remote in sdk.created:
        commands = [kw for cmd, kw in remote.calls if "__prized_c=" in str(cmd)]
        assert len(commands) == 1 and commands[0].get("full_output") is True


async def test_capped_output_says_so(sdk):
    shell = PrizedShellTool()
    await shell.start()
    sdk.created[0].handler = lambda cmd, kw: ExecResult("x" * 10, "", 0, truncated=True)
    result = await shell.run("yes")
    assert result.truncated and "kept only the start of this command's output" in result.stderr
    sdk.created[0].handler = None
    await shell.close()


async def test_output_a_full_disk_cut_says_so(sdk):
    shell = PrizedShellTool()
    await shell.start()
    sdk.created[0].handler = lambda cmd, kw: cut_result("x" * 10, "write_failed")
    result = await shell.run("make")
    assert result.truncated and "its disk is full" in result.stderr
    assert "kept only the start" not in result.stderr
    sdk.created[0].handler = lambda cmd, kw: cut_result("x" * 10, "output_limit")
    assert "kept only the start of this command's output" in (await shell.run("yes")).stderr
    sdk.created[0].handler = None
    await shell.close()


@pytest.mark.parametrize("reason", ["not_found", "network_error"])
async def test_output_that_could_not_be_read_back_is_a_result_and_never_reruns(sdk, reason):
    shell = PrizedShellTool()
    await shell.start()
    calls = []

    def unread(cmd, kw):
        if "__prized_c=" in str(cmd):
            calls.append(cmd)
            raise output_read_error(3, reason)

    sdk.created[0].handler = unread
    result = await shell.run("make deploy")
    assert result.exit_code == 3 and result.stdout == "" and not result.timed_out
    assert f"the command ran, but its output could not be read back ({reason})" in result.stderr
    assert "/home/prized/.prized/exec/x/stdout and /home/prized/.prized/exec/x/stderr" in result.stderr
    assert len(calls) == 1 and len(sdk.created) == 1 and not sdk.created[0].deleted
    sdk.created[0].handler = None
    await shell.close()


# a timeout's TERM ends the command without saving its half-done state.


async def test_terminated_command_does_not_save(sdk):
    shell = PrizedShellTool()
    await shell.run("cd /home/prized")
    result = await shell.run("cd /; export HALF=1; kill -TERM $$; echo never")
    assert result.exit_code == 143
    assert (await shell.run('pwd; echo "${HALF-unset}"')).stdout == "/home/prized\nunset\n"
    await shell.close()


# an on_output callback that raises is logged; the command goes on.


async def test_failing_on_output_is_logged_not_fatal(sdk, caplog):
    def boom(stream: str, text: str) -> None:
        raise RuntimeError("display broke")

    shell = PrizedShellTool(on_output=boom)
    with caplog.at_level(logging.ERROR):
        result = await shell.run("echo a; echo b")
    assert result.stdout == "a\nb\n" and result.exit_code == 0
    assert "on_output callback raised" in caplog.text
    await shell.close()


# a delete is finished even when the caller is cancelled again meanwhile.


async def test_discard_finishes_despite_cancellation(sdk):
    shell = PrizedShellTool()
    await shell.start()
    remote = sdk.created[0]
    handle = shell.sandbox
    gate = asyncio.Event()
    real_delete = handle.delete

    async def slow_delete(**kw):
        await gate.wait()
        await real_delete(**kw)

    handle.delete = slow_delete  # type: ignore[method-assign]
    task = asyncio.ensure_future(shell_module._discard(handle))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()  # a second cancellation
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert remote.deleted


# `set -x` in a command doesn't trace the session's own machinery.


async def test_xtrace_does_not_trace_the_save(sdk):
    shell = PrizedShellTool()
    result = await shell.run("set -x; echo hi")
    assert "__prized" not in result.stderr
    assert result.stdout == "hi\n"
    await shell.close()


async def test_a_removed_default_dir_is_put_back_even_past_a_users_exit_trap(sdk):
    # Some template builds answer nothing once /home/prized/work is gone (the fake does too).
    for mode in ("stateless", "persistent"):
        shell = PrizedShellTool(mode=mode)  # type: ignore[arg-type]
        await shell.run("trap 'echo bye' EXIT; cd /home/prized && rm -rf /home/prized/work")
        assert (await shell.run("echo alive")).stdout == "alive\n", mode
        await shell.close()
