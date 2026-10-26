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

"""PrizedShellTool against the offline fake (real local bash, no network)."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any

import prized
import pytest
from agent_framework import FunctionTool
from agent_framework_tools.shell import (
    ShellCommandError,
    ShellEnvironmentProvider,
    ShellExecutionError,
    ShellExecutor,
    ShellFamily,
    ShellPolicy,
)
from prized import ExecResult, PrizedError

from agent_framework_prized import PrizedShellTool
from agent_framework_prized import _shell as shell_module
from tests.fakes import FakeSdk, Remote

SMALL: dict[str, Any] = {"vcpu": 1, "memory_gb": 2}


@pytest.fixture
def sdk():
    fake = FakeSdk()
    with fake.patch():
        yield fake


def text_of(output: Any) -> str:
    """The text a function result gives the model."""
    if isinstance(output, list):
        return "".join(item.text or "" for item in output)
    return str(output)


def only(sdk: FakeSdk) -> Remote:
    assert len(sdk.created) == 1, [r.id for r in sdk.created]
    return sdk.created[0]


# ---- the framework's contract ----


async def test_satisfies_the_shell_executor_protocol_and_creates_nothing_until_used(sdk):
    shell = PrizedShellTool(**SMALL)
    assert isinstance(shell, ShellExecutor)
    assert sdk.created == []
    assert shell.sandbox_id is None


async def test_as_function_is_a_shell_kind_function_with_a_sandbox_description(sdk):
    fn = PrizedShellTool(approval_mode="never_require", timeout=30).as_function()
    assert isinstance(fn, FunctionTool)
    assert fn.name == "run_shell"
    assert fn.kind == "shell"
    assert fn.approval_mode == "never_require"
    assert "Prized" in fn.description and "/home/prized/work" in fn.description and "30 seconds" in fn.description
    assert "carry over" in fn.description
    stateless = PrizedShellTool(mode="stateless", timeout=None).as_function(name="sh", description=None)
    assert (
        stateless.name == "sh" and "fresh shell" in stateless.description and "no time limit" in stateless.description
    )
    assert PrizedShellTool().as_function().approval_mode == "always_require"
    assert sdk.created == []


async def test_function_runs_the_command_and_returns_the_frameworks_text(sdk):
    shell = PrizedShellTool(approval_mode="never_require")
    fn = shell.as_function()
    rendered = text_of(await fn.invoke(arguments={"command": "echo hi; echo oops >&2; exit 3"}))
    assert "hi" in rendered and "stderr: oops" in rendered and "exit_code: 3" in rendered
    await shell.close()


# ---- lifecycle ----


async def test_first_command_creates_a_sandbox_with_the_defaults(sdk):
    shell = PrizedShellTool(**SMALL, labels={"team": "a"})
    result = await shell.run("echo hello")
    assert (result.stdout, result.exit_code, result.timed_out) == ("hello\n", 0, False)
    remote = only(sdk)
    kw = remote.create_kwargs
    assert kw["template"] == "prized-base-v1"
    assert (kw["vcpu"], kw["memory_gb"]) == (1, 2)
    assert kw["labels"] == {"agent-framework": "shell", "team": "a"}
    assert kw["env_vars"] == {"PIP_BREAK_SYSTEM_PACKAGES": "1"}
    assert (kw["auto_pause_min"], kw["auto_delete_min"], kw["max_runtime_min"]) == (10, 60, None)
    assert shell.sandbox_id == remote.id
    await shell.run("true")
    assert len(sdk.created) == 1
    await shell.close()
    assert remote.deleted
    await shell.close()  # idempotent


async def test_async_with_starts_eagerly_and_deletes(sdk):
    async with PrizedShellTool(**SMALL) as shell:
        remote = only(sdk)
        assert not remote.calls  # started, nothing run yet
        assert shell.sandbox.id == remote.id
    assert remote.deleted
    assert sdk.alive() == []


async def test_keep_or_pause_defaults_never_auto_delete(sdk):
    await PrizedShellTool(on_close="pause").start()
    assert sdk.created[-1].create_kwargs["auto_delete_min"] is None
    await PrizedShellTool(on_close="keep", auto_delete_min=5).start()
    assert sdk.created[-1].create_kwargs["auto_delete_min"] == 5
    for r in sdk.created:
        r.state = "deleted"


async def test_restart_after_delete_makes_a_new_sandbox(sdk):
    shell = PrizedShellTool()
    await shell.run("echo one > f")
    first = only(sdk)
    await shell.close()
    assert first.deleted
    result = await shell.run("cat f || echo missing")
    assert "missing" in result.stdout
    assert len(sdk.created) == 2 and shell.sandbox_id == sdk.created[1].id
    await shell.close()
    assert sdk.alive() == []


async def test_close_with_pause_then_restart_resumes_the_same_sandbox_and_session(sdk):
    shell = PrizedShellTool(on_close="pause")
    await shell.run("mkdir -p app && cd app && export STAGE=two && echo kept > f")
    remote = only(sdk)
    await shell.close()
    assert remote.state == "paused" and remote.pause_kwargs == [{"wait": False}]
    result = await shell.run('pwd; echo "$STAGE"; cat f')
    assert result.stdout == "/home/prized/work/app\ntwo\nkept\n"
    assert len(sdk.created) == 1
    remote.state = "deleted"


async def test_paused_sandbox_gone_by_restart_is_replaced_and_the_model_told(sdk):
    shell = PrizedShellTool(on_close="pause")
    await shell.run("echo x > f")
    await shell.close()
    sdk.created[0].state = "deleted"  # e.g. its auto-delete ran
    result = await shell.run("cat f; echo after")
    assert "fresh, empty sandbox" in result.stderr and "after" in result.stdout
    assert len(sdk.created) == 2
    assert "fresh" not in (await shell.run("true")).stderr  # said once
    sdk.created[1].state = "deleted"


async def test_attach_by_id_keeps_it_and_rejects_create_options(sdk):
    existing = sdk.add()
    shell = PrizedShellTool(sandbox_id=existing.id)
    assert (await shell.run("echo attached")).stdout == "attached\n"
    assert sdk.created == [existing]
    await shell.close()
    assert existing.state == "running"
    with pytest.raises(ValueError, match="sandbox_id"):
        PrizedShellTool(sandbox_id=existing.id, vcpu=2)
    with pytest.raises(ValueError, match="sandbox_id"):
        PrizedShellTool(sandbox_id=existing.id, auto_pause_min=0)
    existing.state = "deleted"


async def test_attach_with_on_close_delete_deletes(sdk):
    existing = sdk.add()
    shell = PrizedShellTool(sandbox_id=existing.id, on_close="delete")
    await shell.start()
    await shell.close()
    assert existing.deleted


async def test_attached_sandbox_that_is_gone_is_an_error_not_a_new_sandbox(sdk):
    existing = sdk.add()
    existing.state = "deleted"
    shell = PrizedShellTool(sandbox_id=existing.id)
    with pytest.raises(ShellExecutionError, match="gone"):
        await shell.run("true")
    assert sdk.created == [existing]


async def test_attached_sandbox_deleted_while_in_use_is_an_error(sdk):
    existing = sdk.add()
    shell = PrizedShellTool(sandbox_id=existing.id)
    await shell.run("true")
    existing.state = "deleted"
    with pytest.raises(ShellExecutionError):
        await shell.run("true")
    assert sdk.created == [existing]


async def test_options_are_checked(sdk):
    for bad in (
        {"mode": "sticky"},
        {"approval_mode": "sometimes"},
        {"on_close": "stop"},
        {"working_dir": "work"},
        {"timeout": 0},
        {"timeout": -1},
        {"max_output_bytes": 0},
        {"sandbox_id": ""},
    ):
        with pytest.raises(ValueError):
            PrizedShellTool(**bad)


async def test_gone_owned_sandbox_is_replaced_once_and_the_model_told(sdk, caplog):
    shell = PrizedShellTool(**SMALL)
    await shell.run("echo 1 > f")
    first = only(sdk)
    first.state = "deleted"  # its run time ended
    with caplog.at_level(logging.WARNING):
        result = await shell.run("ls; echo ok")
    assert result.stdout == "ok\n"
    assert result.stderr.startswith("[prized: the previous sandbox was gone")
    assert "created a fresh one" in caplog.text
    second = sdk.created[1]
    assert second.create_kwargs["vcpu"] == 1 and shell.sandbox_id == second.id
    assert (await shell.run("echo again")).stderr == ""
    await shell.close()
    assert sdk.alive() == []


async def test_sandbox_in_error_state_is_deleted_and_replaced(sdk):
    shell = PrizedShellTool()
    await shell.run("true")
    first = only(sdk)
    first.handler = lambda cmd, kw: (_ for _ in ()).throw(PrizedError("sandbox_not_running", status=409))
    first.state = "error"
    result = await shell.run("echo fine")
    assert result.stdout == "fine\n"
    assert first.deleted  # a failed sandbox still counts toward quota
    await shell.close()
    assert sdk.alive() == []


async def test_other_api_errors_are_shell_execution_errors_without_replacing(sdk):
    shell = PrizedShellTool()
    await shell.run("true")
    remote = only(sdk)
    remote.handler = lambda cmd, kw: (_ for _ in ()).throw(PrizedError("rate_limited", status=429))
    with pytest.raises(ShellExecutionError, match="rate_limited") as info:
        await shell.run("true")
    assert isinstance(info.value.__cause__, PrizedError)
    assert len(sdk.created) == 1
    remote.handler = None
    await shell.close()


async def test_failed_create_raises_and_a_later_command_retries(sdk):
    sdk.fail_creates = 1
    shell = PrizedShellTool()
    with pytest.raises(ShellExecutionError, match="sandbox_capacity"):
        await shell.run("true")
    assert sdk.created == []
    assert (await shell.run("echo up")).stdout == "up\n"
    await shell.close()
    assert sdk.alive() == []


async def test_failed_delete_raises_and_close_can_be_retried(sdk):
    shell = PrizedShellTool()
    await shell.run("true")
    remote = only(sdk)
    remote.fail_delete = 1
    with pytest.raises(PrizedError):
        await shell.close()
    assert not remote.deleted
    await shell.close()
    assert remote.deleted


async def test_close_of_a_sandbox_already_gone_is_fine(sdk):
    shell = PrizedShellTool()
    await shell.run("true")
    only(sdk).state = "deleted"
    await shell.close()
    assert sdk.alive() == []


async def test_custom_working_dir_is_created(sdk):
    shell = PrizedShellTool(working_dir="/home/prized/project/")
    assert shell.working_dir == "/home/prized/project"
    assert (await shell.run("pwd")).stdout == "/home/prized/project\n"
    await shell.close()


# ---- commands ----


async def test_persistent_session_keeps_cwd_variables_and_functions(sdk):
    shell = PrizedShellTool()
    await shell.run("mkdir -p a/b && cd a/b && export TOKEN='x y' && COUNT=3 && greet() { echo hi \"$1\"; }")
    result = await shell.run('pwd; echo "$TOKEN" "$COUNT"; greet you')
    assert result.stdout == "/home/prized/work/a/b\nx y 3\nhi you\n"
    # An exit, a failure and a syntax error leave the session as it was.
    assert (await shell.run("cd /home/prized; exit 7")).exit_code == 7
    assert (await shell.run("pwd")).stdout == "/home/prized\n"
    assert (await shell.run("if then")).exit_code == 2
    assert (await shell.run("pwd; echo $COUNT")).stdout == "/home/prized\n3\n"
    await shell.close()


async def test_persistent_session_supports_venv_activate_and_deactivate(sdk):
    shell = PrizedShellTool()
    await shell.run("python3 -m venv --without-pip v && . v/bin/activate")
    assert (await shell.run("command -v python")).stdout == "/home/prized/work/v/bin/python\n"
    await shell.run("deactivate")
    assert "/v/bin" not in (await shell.run("echo $PATH")).stdout
    await shell.close()


async def test_stateless_mode_starts_fresh_but_keeps_files(sdk):
    shell = PrizedShellTool(mode="stateless")
    await shell.run("mkdir sub && cd sub && export X=1 && echo data > f")
    result = await shell.run('pwd; echo "x=${X-unset}"; cat sub/f')
    assert result.stdout == "/home/prized/work\nx=unset\ndata\n"
    assert not only(sdk).exists("/home/prized/.prized/maf/shell-state.sh")
    await shell.close()


async def test_pip_user_bin_is_on_path_and_pip_may_install(sdk):
    for mode in ("persistent", "stateless"):
        shell = PrizedShellTool(mode=mode, env={"EXTRA": "e"})
        result = await shell.run('echo "$PATH"; echo "$PIP_BREAK_SYSTEM_PACKAGES $EXTRA"')
        path, flags = result.stdout.splitlines()
        assert path.split(":")[0] == "/home/prized/.local/bin", mode
        assert flags == "1 e"
        await shell.close()


async def test_timeout_returns_what_was_printed_and_the_session_carries_on(sdk):
    shell = PrizedShellTool(timeout=0.5)
    await shell.run("cd /home/prized && export A=1")
    result = await shell.run("echo started; cd /; sleep 5; echo never")
    assert result.timed_out and result.exit_code == -1
    assert result.stdout == "started\n"
    assert "command timed out" in result.format_for_model()
    after = await shell.run("pwd; echo $A")
    assert after.stdout == "/home/prized\n1\n"  # the killed command's state was not saved
    longer = await shell.run("sleep 0.8; echo done", timeout=5)
    assert longer.stdout == "done\n" and not longer.timed_out
    await shell.close()


async def test_no_timeout_sends_zero(sdk):
    shell = PrizedShellTool(timeout=None)
    await shell.run("true")
    assert only(sdk).calls[-1][1]["timeout"] == 0
    await shell.run("true", timeout=7)
    assert only(sdk).calls[-1][1]["timeout"] == 7
    with pytest.raises(ValueError):
        await shell.run("true", timeout=-1)
    await shell.close()


async def test_output_keeps_head_and_tail(sdk):
    shell = PrizedShellTool(max_output_bytes=100)
    result = await shell.run("for i in $(seq 1 200); do echo line$i; done")
    assert result.truncated
    assert result.stdout.startswith("line1\nline2\n")
    assert result.stdout.endswith("line199\nline200\n")
    assert "[... truncated" in result.stdout
    assert len(result.stdout.encode()) < 160
    small = await shell.run("echo short")
    assert not small.truncated
    await shell.close()


async def test_prized_output_cap_marks_the_result_truncated(sdk):
    shell = PrizedShellTool()
    await shell.start()
    only(sdk).handler = lambda cmd, kw: ExecResult("x" * 10, "", 0, truncated=True)
    assert (await shell.run("yes")).truncated
    only(sdk).handler = None
    await shell.close()


async def test_a_command_too_long_for_one_argument_is_staged_and_cleaned_up(sdk):
    shell = PrizedShellTool()
    payload = "z" * 200_000
    result = await shell.run(f"cd /home/prized && printf '%s' '{payload}' > big.txt && wc -c < big.txt")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "200000"
    remote = only(sdk)
    assert (await shell.run("pwd")).stdout == "/home/prized\n"  # the session state was kept
    assert [n for n in (await shell.run("ls /home/prized/.prized/maf")).stdout.split() if n.startswith("cmd-")] == []
    stateless = PrizedShellTool(sandbox_id=remote.id, mode="stateless")
    result = await stateless.run(f"echo '{payload}' | wc -c")
    assert result.stdout.strip() == "200001"
    await stateless.close()
    await shell.close()


async def test_policy_refuses_before_running_and_the_function_tells_the_model(sdk):
    seen: list[str] = []
    shell = PrizedShellTool(policy=ShellPolicy(denylist=[r"\brm\b"]), on_command=seen.append)
    with pytest.raises(ShellCommandError):
        await shell.run("rm -rf /")
    assert sdk.created == []  # refused before anything started
    text = text_of(await shell.as_function().invoke(arguments={"command": "rm x"}))
    assert "rejected by policy" in text
    await shell.run("echo allowed")
    assert seen == ["echo allowed"]
    await shell.close()


async def test_a_failing_on_command_hook_is_logged_not_raised(sdk, caplog):
    def boom(_: str) -> None:
        raise RuntimeError("hook")

    shell = PrizedShellTool(on_command=boom)
    with caplog.at_level(logging.ERROR):
        assert (await shell.run("echo ok")).stdout == "ok\n"
    assert "on_command hook raised" in caplog.text
    await shell.close()


async def test_on_output_streams_and_may_be_async(sdk):
    chunks: list[tuple[str, str]] = []
    shell = PrizedShellTool(on_output=lambda stream, text: chunks.append((stream, text)))
    result = await shell.run("echo out; echo err >&2")
    assert ("stdout", "out\n") in chunks and ("stderr", "err\n") in chunks
    assert result.stdout == "out\n"
    seen: list[str] = []

    async def collect(stream: str, text: str) -> None:
        await asyncio.sleep(0)
        seen.append(text)

    await shell.run("echo per-call", on_output=collect)
    assert seen == ["per-call\n"]
    await shell.close()


async def test_persistent_commands_take_turns_stateless_ones_do_not(sdk):
    shell = PrizedShellTool()
    await asyncio.gather(*(shell.run("sleep 0.2") for _ in range(3)))
    assert only(sdk).max_running == 1
    await shell.close()
    stateless = PrizedShellTool(mode="stateless")
    await asyncio.gather(*(stateless.run("sleep 0.2") for _ in range(3)))
    assert sdk.created[1].max_running == 3
    await stateless.close()


# ---- event loops and threads ----


def test_each_event_loop_gets_its_own_handle_on_one_sandbox(sdk):
    shell = PrizedShellTool()
    assert asyncio.run(shell.run("echo 1 > f && cd /home/prized")).exit_code == 0
    result = asyncio.run(shell.run("cat work/f; pwd"))
    assert result.stdout == "1\n/home/prized\n"
    assert len(sdk.created) == 1 and sdk.connects == [sdk.created[0].id]
    asyncio.run(shell.close())
    assert sdk.alive() == []


def test_threads_starting_at_once_share_one_sandbox(sdk):
    sdk.create_delay = 0.05
    shell = PrizedShellTool(mode="stateless")
    results: list[Any] = []
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            results.append(asyncio.run(shell.run(f"echo {n}")))
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(r.stdout for r in results) == [f"{n}\n" for n in range(6)]
    assert len(sdk.alive()) == 1  # any extra sandbox a losing thread made was deleted
    asyncio.run(shell.close())
    assert sdk.alive() == []


def test_threads_replacing_a_gone_sandbox_make_one_replacement(sdk):
    shell = PrizedShellTool(mode="stateless")
    asyncio.run(shell.run("true"))
    sdk.created[0].state = "deleted"
    sdk.create_delay = 0.05
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            asyncio.run(shell.run("true"))
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(sdk.alive()) == 1
    asyncio.run(shell.close())
    assert sdk.alive() == []


async def test_close_while_starting_leaves_no_sandbox(sdk):
    sdk.create_delay = 0.1
    shell = PrizedShellTool()
    run = asyncio.ensure_future(shell.run("true"))
    await asyncio.sleep(0.02)
    await shell.close()  # before the create finished
    with pytest.raises(ShellExecutionError, match="closed"):
        await run
    assert sdk.alive() == []


def test_close_from_another_thread_while_a_command_runs(sdk):
    shell = PrizedShellTool()
    started = threading.Event()
    outcome: list[Any] = []

    def worker() -> None:
        async def go() -> Any:
            await shell.start()
            started.set()
            return await shell.run("sleep 0.3; echo late")

        try:
            outcome.append(asyncio.run(go()))
        except BaseException as e:
            outcome.append(e)

    t = threading.Thread(target=worker)
    t.start()
    started.wait(5)
    asyncio.run(shell.close())
    t.join()
    assert sdk.alive() == []  # nothing was re-created behind the close


# ---- more ----


async def test_fork_copies_files_and_session_into_tools_with_the_same_settings(sdk):
    shell = PrizedShellTool(**SMALL, timeout=9, mode="persistent", approval_mode="never_require")
    await shell.run("mkdir -p d && cd d && export K=v && echo base > f")
    copies = await shell.fork(2)
    assert len(copies) == 2
    for copy in copies:
        result = await copy.run('pwd; echo "$K"; cat f')
        assert result.stdout == "/home/prized/work/d\nv\nbase\n"
        assert copy.as_function().approval_mode == "never_require"
        assert copy._timeout == 9
    await copies[0].run("echo changed > f")
    assert (await shell.run("cat f")).stdout == "base\n"
    # A copy whose sandbox is gone gets one shaped like the parent's.
    kid = sdk.find(copies[1].sandbox_id)
    assert kid is not None
    kid.state = "deleted"
    await copies[1].run("true")
    assert sdk.created[-1].create_kwargs["vcpu"] == 1
    for copy in copies:
        await copy.close()
    await shell.close()
    assert sdk.alive() == []


async def test_pause_pauses_and_the_next_command_resumes(sdk):
    shell = PrizedShellTool()
    await shell.start()
    await shell.pause()
    assert only(sdk).state == "paused"
    only(sdk).state = "running"  # the API resumes it on the next request
    assert (await shell.run("echo back")).stdout == "back\n"
    await shell.close()


async def test_sandbox_property_needs_a_start(sdk):
    shell = PrizedShellTool()
    with pytest.raises(RuntimeError, match="not started"):
        _ = shell.sandbox
    await shell.start()
    assert isinstance(shell.sandbox.id, str)
    await shell.close()


async def test_environment_options_describe_the_sandbox_even_on_windows(sdk, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    shell = PrizedShellTool()
    options = shell.environment_options()
    assert options.override_family is ShellFamily.POSIX
    provider = ShellEnvironmentProvider(shell, options)
    snapshot = await provider.refresh()
    assert snapshot.family is ShellFamily.POSIX
    assert snapshot.working_directory == "/home/prized/work"
    assert snapshot.shell_version and snapshot.shell_version[0].isdigit()
    text = options.instructions_formatter(snapshot)
    assert "Prized sandbox" in text and "POSIX shell" in text and "PowerShell" not in text
    assert "Working directory: /home/prized/work" in text
    await shell.close()


async def test_closed_handles_are_not_reused_after_close(sdk):
    shell = PrizedShellTool(on_close="keep")
    await shell.run("true")
    remote = only(sdk)
    await shell.close()
    assert all(h.closed for h in remote.handles)
    assert (await shell.run("echo reattached")).stdout == "reattached\n"
    assert len(sdk.created) == 1
    remote.state = "deleted"


def test_generated_scripts_parse_for_any_working_dir(tmp_path):
    import subprocess

    for workdir in ("/home/prized/work", "/home/prized/my project", "/home/prized/it's", "/tmp/$HOME`x`"):
        for build in (shell_module._persistent_script, shell_module._stateless_script):
            script = tmp_path / "s.sh"
            script.write_text(build(workdir, "echo hi"))
            assert subprocess.run(["bash", "-n", str(script)]).returncode == 0, (workdir, build)


async def test_unknown_create_arguments_would_fail_the_fake(sdk):
    with pytest.raises(TypeError):
        await prized.AsyncSandbox.create(bogus=1)
