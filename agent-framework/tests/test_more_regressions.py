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

"""More regression tests for PrizedShellTool and PrizedFileStore: each failed before its fix."""

from __future__ import annotations

import pytest
from agent_framework_tools.shell import ShellExecutionError
from prized import ExecResult

from agent_framework_prized import PrizedFileStore, PrizedShellTool
from agent_framework_prized import _files as files_module
from tests.fakes import FakeSdk

STATE = "/home/prized/.prized/maf/shell-state.sh"


@pytest.fixture
def sdk():
    fake = FakeSdk()
    with fake.patch():
        yield fake


# in persistent mode a command's own EXIT trap never runs when the command
# reaches its end: the trailer's __prized_save does `trap - EXIT` and then `exit`.


async def test_users_exit_trap_runs_in_persistent_mode(sdk):
    shell = PrizedShellTool()
    result = await shell.run("trap 'echo cleanup-ran' EXIT; echo body")
    assert result.stdout == "body\ncleanup-ran\n"  # stateless mode and plain bash print both
    await shell.close()


async def test_users_exit_trap_cleanup_happens(sdk):
    shell = PrizedShellTool()
    await shell.run("mkdir -p /home/prized/scratch; trap 'rm -rf /home/prized/scratch' EXIT; echo working")
    assert not sdk.created[0].exists("/home/prized/scratch")
    await shell.close()


# the tool's own PATH tweak makes PATH look "changed by a command", so PATH is
# written to the state file on every command and a later tool's env={"PATH": ...} never wins.


async def test_env_path_of_a_later_tool_wins(sdk):
    first = PrizedShellTool(on_close="keep")
    await first.run("true")  # never touches PATH
    remote = sdk.created[0]
    await first.close()
    second = PrizedShellTool(sandbox_id=remote.id, env={"PATH": "/opt/tools/bin:/usr/bin:/bin"})
    result = await second.run('echo "$PATH"')
    assert "/opt/tools/bin" in result.stdout, result.stdout
    await second.close()
    remote.state = "deleted"


# a working_dir the sandbox user can't create (e.g. DockerShellTool's /workspace)
# no longer fails: cwd is "/" and `_enter` falls through, so commands run in "/".


async def test_uncreatable_working_dir_does_not_silently_run_in_root(sdk):
    shell = PrizedShellTool(working_dir="/proc/prized-cannot-exist", mode="stateless")
    try:
        result = await shell.run("pwd")
    except ShellExecutionError:
        pass  # failing loudly is fine
    else:
        assert result.exit_code != 0, (result.stdout, result.stderr)
    await shell.close()


# paths that skip the end-of-command repair leave /home/prized/work missing,
# and sandboxes from some template builds (and the fake) then refuse every later call.


@pytest.mark.parametrize("mode", ["persistent", "stateless"])
@pytest.mark.xfail(
    strict=True,
    reason="known: a command that sets its own EXIT trap and calls exit after removing /home/prized/work "
    "leaves a sandbox from those template builds unusable",
)
async def test_repair_survives_user_exit_trap_plus_exit(sdk, mode):
    shell = PrizedShellTool(mode=mode)
    await shell.run("trap 'echo bye' EXIT; cd /home/prized; rm -rf /home/prized/work; exit 0")
    assert (await shell.run("echo alive")).stdout == "alive\n"
    await shell.close()


async def test_repair_survives_term(sdk):
    # A detached run's timeout watchdog sends TERM first; the TERM trap exits without repairing.
    shell = PrizedShellTool()
    await shell.run("cd /home/prized; rm -rf /home/prized/work; kill -TERM $$")
    assert (await shell.run("echo alive")).stdout == "alive\n"
    await shell.close()


# an unterminated heredoc swallows the trailer, so the tool's internal save line
# (expanded) is written into the user's file.


async def test_unterminated_heredoc_does_not_write_internals_into_the_file(sdk):
    shell = PrizedShellTool()
    await shell.run("cat > f.py <<EOF\nprint(1)\n  EOF")
    assert "__prized" not in sdk.created[0].read("/home/prized/work/f.py")
    await shell.close()


# `exit` inside a function saves that function's locals as globals.


async def test_exit_inside_function_does_not_leak_locals(sdk):
    shell = PrizedShellTool()
    await shell.run("f() { local tmp_secret=1; exit 0; }; f")
    assert (await shell.run('echo "${tmp_secret-unset}"')).stdout == "unset\n"
    await shell.close()


# bash's messages and $LINENO are off by one (the prelude is line 1); `set -v`
# echoes the trailer.


async def test_line_numbers_match_the_command(sdk):
    shell = PrizedShellTool()
    result = await shell.run("no_such_cmd_xyz")
    assert "line 1:" in result.stderr, result.stderr
    await shell.close()


async def test_set_v_does_not_echo_internals(sdk):
    shell = PrizedShellTool()
    result = await shell.run("set -v; echo hi")
    assert "__prized" not in result.stderr, result.stderr
    await shell.close()


# a command that leaves PATH without mv/mkdir loses all its session state
# (and the first one prints an internal error).


async def test_state_saved_even_if_command_breaks_path(sdk):
    shell = PrizedShellTool()
    result = await shell.run("cd /home/prized; export KEEP=1; export PATH=/nonexistent")
    assert "shell-state" not in result.stderr, result.stderr
    assert (await shell.run('pwd; echo "${KEEP-unset}"')).stdout == "/home/prized\n1\n"
    await shell.close()


# export -f and export -n are not kept.


async def test_exported_function_stays_exported(sdk):
    shell = PrizedShellTool()
    await shell.run("g() { echo g-ok; }; export -f g")
    assert (await shell.run("bash -c g")).stdout == "g-ok\n"
    await shell.close()


# ---- PrizedFileStore ----

# a failed overwrite truncates the existing file: `cat -- staged > file` empties
# it before cat runs, and nothing restores it (stands in for ENOSPC/EIO mid-copy).


async def test_failed_overwrite_keeps_the_old_content(sdk):
    import os

    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await store.write("precious.txt", "years of work")
    remote = sdk.created[0]

    def drop_staged(cmd, kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["bash", "-c", files_module._WRITE]:
            os.remove(remote.local(cmd[-1]))

    remote.handler = drop_staged
    with pytest.raises(OSError):
        await store.write("precious.txt", "new content")
    remote.handler = None
    assert remote.read("/home/prized/work/precious.txt") == "years of work"
    await shell.close()


# root="/" refuses every path (the containment pattern becomes "//*").


async def test_root_slash_works(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell, root="/")
    await shell.run("echo hi > /home/prized/work/x.txt")
    assert await store.file_exists("home/prized/work/x.txt") is True
    await shell.close()


# writing to an existing FIFO blocks until the helper's 120 s timeout
# (noclobber only refuses regular files; `cat >` opens the pipe).


async def test_write_to_fifo_fails_fast(sdk, monkeypatch):
    monkeypatch.setattr(files_module, "_HELPER_TIMEOUT", 3)
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await shell.run("mkfifo pipe")
    with pytest.raises(OSError):
        await store.write("pipe", "x", overwrite=False)
    await shell.close()


# a listing cut by the sandbox's 4 MiB output cap is returned as if complete,
# its last name possibly cut mid-way.


async def test_truncated_listing_is_not_returned_as_complete(sdk):
    shell = PrizedShellTool()
    store = PrizedFileStore(shell)
    await shell.run("true")
    remote = sdk.created[0]
    remote.handler = lambda cmd, kw: ExecResult("f/aaa\0f/bbb\0f/cc", "", 0, truncated=True)
    with pytest.raises(OSError):
        await store.list_children()
    remote.handler = None
    await shell.close()


async def test_users_exit_trap_stops_a_test_server(sdk):
    # The common "start a server, trap kill on exit, curl it" pattern leaves the server running.
    shell = PrizedShellTool()
    await shell.run("sleep 300 >/dev/null 2>&1 & echo $! > /home/prized/pid; trap 'kill $(cat /home/prized/pid)' EXIT")
    still = await shell.run("kill -0 $(cat /home/prized/pid) 2>/dev/null && echo running || echo stopped")
    await shell.run("kill $(cat /home/prized/pid) 2>/dev/null; true")  # tidy up the local sleep
    assert still.stdout == "stopped\n"
    await shell.close()
