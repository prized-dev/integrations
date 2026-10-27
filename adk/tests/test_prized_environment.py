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

"""Tests for PrizedEnvironment (no network: the Prized API is faked)."""

import asyncio
import logging
from unittest import mock

from fakes import cut_result
from fakes import FakeSdk
from fakes import FALLBACK_PATH
from fakes import output_read_error
from google.adk.tools.environment import EnvironmentToolset
from prized import CommandTimeoutError
from prized import ExecResult
from prized import PrizedError
import pytest

from prized_adk import PrizedEnvironment

STAGING = "/home/prized/.prized/adk/"


@pytest.fixture(name="sdk")
def _sdk():
  sdk = FakeSdk()
  with sdk.patch():
    yield sdk


def only(match, result):
  """A handler for commands containing ``match``; others run as usual."""

  def handler(cmd, kwargs):
    return result(cmd, kwargs) if match in str(cmd) else None

  return handler


def user_commands(box):
  return [(c, kw) for c, kw in box.calls if c != ["printenv", "PATH"]]


# ---- lifecycle ---------------------------------------------------------------


async def test_initialize_creates_sandbox_with_options(sdk):
  env = PrizedEnvironment(
      template="prized-agent-v1",
      vcpu=2,
      memory_gb=4,
      env_vars={"A": "1"},
      labels={"team": "x"},
  )
  assert env._is_initialized is False
  await env.initialize()
  assert env._is_initialized is True

  (box,) = sdk.created
  assert box.create_kwargs["template"] == "prized-agent-v1"
  assert box.create_kwargs["vcpu"] == 2
  assert box.create_kwargs["memory_gb"] == 4
  assert box.create_kwargs["env_vars"] == {
      "PIP_BREAK_SYSTEM_PACKAGES": "1",
      "A": "1",
  }
  assert box.create_kwargs["labels"] == {"adk": "environment", "team": "x"}
  assert box.create_kwargs["auto_pause_min"] == 10
  assert box.create_kwargs["auto_delete_min"] == 60
  assert box.create_kwargs["max_runtime_min"] is None
  assert env.sandbox_id == box.id
  assert str(env.working_dir) == "/home/prized/work"


async def test_auto_delete_defaults_follow_on_close(sdk):
  await PrizedEnvironment().initialize()
  await PrizedEnvironment(on_close="pause").initialize()
  await PrizedEnvironment(on_close="keep", auto_delete_min=5).initialize()
  assert [b.create_kwargs["auto_delete_min"] for b in sdk.created] == [
      60,
      None,
      5,
  ]


async def test_initialize_is_idempotent_and_safe_in_parallel(sdk):
  env = PrizedEnvironment()
  await asyncio.gather(*(env.initialize() for _ in range(5)))
  await env.initialize()
  assert len(sdk.created) == 1  # the fake's create yields: a race would show


async def test_close_deletes_created_sandbox_and_is_idempotent(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  box = sdk.created[0]
  await env.close()
  assert box.deleted
  assert all(h.closed for h in box.handles)
  assert env._is_initialized is False
  await env.close()
  # The id stays readable for logs and later attaches.
  assert env.sandbox_id == box.id


async def test_a_failed_close_can_be_retried(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  box = sdk.created[0]
  box.fail_delete = 1
  with pytest.raises(PrizedError):
    await env.close()
  assert not box.deleted
  await env.close()
  assert box.deleted


async def test_close_pause_keeps_sandbox_for_a_later_attach(sdk):
  env = PrizedEnvironment(on_close="pause")
  await env.initialize()
  await env.write_file("notes.txt", "kept")
  await env.close()
  box = sdk.created[0]
  assert box.state == "paused"

  again = PrizedEnvironment(sandbox_id=env.sandbox_id)
  await again.initialize()
  assert await again.read_file("notes.txt") == b"kept"
  await again.close()
  # An attached sandbox is kept by default.
  assert box.state == "paused"
  assert len(sdk.created) == 1


async def test_reinitialize_after_a_pause_resumes_the_same_sandbox(sdk):
  """An `adk web` reload: close() then initialize() on the same toolset."""
  env = PrizedEnvironment(on_close="pause")
  await env.initialize()
  await env.write_file("notes.txt", "kept")
  await env.close()
  await env.initialize()
  assert len(sdk.created) == 1
  assert await env.read_file("notes.txt") == b"kept"
  sdk.created[0].state = "deleted"  # gone meanwhile: a fresh one, said so
  await env.close()
  await env.initialize()
  assert len(sdk.created) == 2
  assert "fresh, empty sandbox" in (await env.execute("ls")).stderr


async def test_attach_to_a_missing_sandbox_raises(sdk):
  with pytest.raises(RuntimeError, match="gone"):
    await PrizedEnvironment(sandbox_id="sbx_nope").initialize()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sandbox_id": "sbx_1", "template": "prized-agent-v1"},
        {"sandbox_id": "sbx_1", "vcpu": 2},
        {"sandbox_id": "sbx_1", "labels": {"a": "b"}},
        {"sandbox_id": "sbx_1", "auto_delete_min": 5},
        {"sandbox_id": "sbx_1", "auto_pause_min": 5},
        {"sandbox_id": "sbx_1", "max_runtime_min": 5},
        {"on_close": "stop"},
        {"working_dir": "work"},
    ],
)
def test_invalid_options_raise(kwargs):
  with pytest.raises(ValueError):
    PrizedEnvironment(**kwargs)


async def test_working_dir_and_execute_require_initialize():
  env = PrizedEnvironment()
  with pytest.raises(RuntimeError):
    _ = env.working_dir
  with pytest.raises(RuntimeError):
    await env.execute("echo hi")


async def test_custom_working_dir_is_created(sdk):
  env = PrizedEnvironment(working_dir="/home/prized/project/")
  await env.initialize()
  box = sdk.created[0]
  assert ["mkdir", "-p", "--", "/home/prized/project"] in [
      c for c, _ in box.calls
  ]
  assert str(env.working_dir) == "/home/prized/project"


# ---- commands ----------------------------------------------------------------


async def test_execute_success_runs_in_working_dir(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  box = sdk.created[0]
  box.handler = only("echo out", lambda cmd, kw: ExecResult("out", "err", 0))

  result = await env.execute("echo out", timeout=30)

  assert (result.exit_code, result.stdout, result.stderr) == (0, "out", "err")
  assert result.timed_out is False
  cmd, kwargs = user_commands(box)[-1]
  assert cmd == "echo out"
  assert kwargs == {
      "cwd": "/home/prized/work",
      # pip may install without root, and what it installs is on PATH.
      "env": {
          "PIP_BREAK_SYSTEM_PACKAGES": "1",
          "PATH": "/home/prized/.local/bin:" + FALLBACK_PATH,
      },
      "timeout": 30,
      # The whole output, not just the first 4 MiB of each stream.
      "full_output": True,
  }


async def test_execute_without_timeout_has_no_limit(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  await env.execute("make")
  assert user_commands(sdk.created[0])[-1][1]["timeout"] == 0


async def test_execute_nonzero_exit_is_normal_result(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  sdk.created[0].handler = only(
      "false", lambda cmd, kw: ExecResult("partial", "boom", 2)
  )

  result = await env.execute("false")

  assert (result.exit_code, result.stdout, result.stderr) == (
      2,
      "partial",
      "boom",
  )


async def test_execute_timeout_keeps_partial_output(sdk):
  env = PrizedEnvironment()
  await env.initialize()

  def slow(cmd, kwargs):
    raise CommandTimeoutError(
        ExecResult("tick\n", "", -1, timed_out=True), kwargs["timeout"]
    )

  sdk.created[0].handler = only("sleep", slow)
  result = await env.execute("sleep 999", timeout=1)

  assert result.timed_out is True
  assert result.exit_code == -1
  assert result.stdout == "tick\n"


async def test_execute_says_when_the_output_was_cut_and_why(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  sdk.created[0].handler = only(
      "yes", lambda cmd, kw: cut_result("y\n", "output_limit")
  )
  result = await env.execute("yes")
  assert result.stdout == "y\n"
  assert "kept only the start of this command's output" in result.stderr
  sdk.created[0].handler = only(
      "make", lambda cmd, kw: cut_result("cc\n", "write_failed")
  )
  result = await env.execute("make")
  assert "its disk is full" in result.stderr
  assert "kept only the start" not in result.stderr


async def test_execute_timeout_says_its_partial_output_was_cut(sdk):
  env = PrizedEnvironment()
  await env.initialize()

  def slow(cmd, kwargs):
    raise CommandTimeoutError(cut_result("tick\n", "write_failed"), 1)

  sdk.created[0].handler = only("sleep", slow)
  result = await env.execute("sleep 999", timeout=1)
  assert result.timed_out is True and result.stdout == "tick\n"
  assert "its disk is full" in result.stderr


@pytest.mark.parametrize("reason", ["not_found", "sandbox_not_running"])
async def test_output_that_could_not_be_read_back_is_a_result_and_never_reruns(
    sdk, reason
):
  env = PrizedEnvironment()
  await env.initialize()

  def unread(cmd, kwargs):
    raise output_read_error(4, reason)

  sdk.created[0].handler = only("make deploy", unread)
  result = await env.execute("make deploy")

  assert result.exit_code == 4 and result.stdout == ""
  assert (
      f"the command ran, but its output could not be read back ({reason})"
      in result.stderr
  )
  assert "/home/prized/.prized/exec/x/stdout and" in result.stderr
  box = sdk.created[0]
  assert [c for c, _ in user_commands(box)].count("make deploy") == 1
  assert len(sdk.created) == 1


# ---- files -------------------------------------------------------------------


async def test_read_and_write_inside_home(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  box = sdk.created[0]

  await env.write_file("sub/out.txt", "hello")
  await env.write_file("/home/prized/raw.bin", b"\x00\x01")

  assert box.fs["/home/prized/work/sub/out.txt"] == b"hello"
  assert await env.read_file("sub/out.txt") == b"hello"
  assert await env.read_file("/home/prized/raw.bin") == b"\x00\x01"
  assert await env.read_file("sub/../sub/out.txt") == b"hello"
  # The staging files went away with the helper's trap.
  assert not [p for p in box.fs if p.startswith(STAGING)]


async def test_writes_go_into_the_existing_file(sdk):
  """`cat >`: an API upload would replace the file with mode 0600."""
  env = PrizedEnvironment()
  await env.initialize()
  await env.write_file("run.sh", "echo 1\n")
  writes = [
      c
      for c, _ in sdk.created[0].calls
      if isinstance(c, list) and c[:2] == ["bash", "-c"]
  ]
  assert writes[-1][-1] == "/home/prized/work/run.sh"
  assert 'cat -- "$1" > "$2"' in writes[-1][2]


async def test_symlinks_go_through_the_shell(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  box = sdk.created[0]
  box.fs["/home/prized/work/AGENTS.md"] = b"rules"
  box.symlinks["/home/prized/work/CLAUDE.md"] = "/home/prized/work/AGENTS.md"
  assert await env.read_file("CLAUDE.md") == b"rules"
  await env.write_file("CLAUDE.md", "new rules")
  assert box.fs["/home/prized/work/AGENTS.md"] == b"new rules"


async def test_read_missing_file_raises_file_not_found(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  with pytest.raises(FileNotFoundError):
    await env.read_file("missing.txt")
  # The sandbox was fine: nothing was replaced.
  assert len(sdk.created) == 1


async def test_read_directory_raises_file_not_found(sdk):
  """ADK's file tools report a FileNotFoundError; anything else ends the run."""
  env = PrizedEnvironment()
  await env.initialize()
  sdk.created[0].dirs.add("/tmp/dir")
  with pytest.raises(FileNotFoundError):
    await env.read_file("/home/prized/work")
  with pytest.raises(FileNotFoundError):
    await env.read_file("/tmp/dir")


async def test_read_outside_home_goes_through_the_shell(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  box = sdk.created[0]
  box.fs["/etc/hostname"] = b"sandbox\n"

  assert await env.read_file("/etc/hostname") == b"sandbox\n"
  assert box.calls[-1][0] == ["base64", "-w0", "--", "/etc/hostname"]
  with pytest.raises(FileNotFoundError):
    await env.read_file("/etc/missing")


async def test_read_outside_home_too_large_raises(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  sdk.created[0].handler = only(
      "base64", lambda cmd, kw: ExecResult("QUFB", "", 0, truncated=True)
  )
  with pytest.raises(OSError, match="too large"):
    await env.read_file("/var/log/big.log")


async def test_write_outside_home_and_permission_errors(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  box = sdk.created[0]

  await env.write_file("/tmp/data/x.txt", "outside")

  assert box.fs["/tmp/data/x.txt"] == b"outside"
  with pytest.raises(PermissionError):
    await env.write_file("/root/x.txt", "no")
  assert not [p for p in box.fs if p.startswith(STAGING)]


# ---- a sandbox that is gone --------------------------------------------------


async def test_gone_sandbox_is_replaced_and_the_model_is_told(sdk, caplog):
  env = PrizedEnvironment()
  await env.initialize()
  sdk.created[0].state = "deleted"  # its run time ended

  with caplog.at_level(logging.WARNING):
    result = await env.execute("echo hi")

  assert result.exit_code == 0
  assert len(sdk.created) == 2
  assert env.sandbox_id == sdk.created[1].id
  assert "is gone" in caplog.text
  assert "fresh, empty sandbox" in result.stderr
  assert "fresh" not in (await env.execute("echo again")).stderr  # told once


def test_gone_sandbox_is_replaced_on_a_new_event_loop(sdk):
  """Sync Runner.run starts a new loop per call; the reconnect finds it gone."""
  env = PrizedEnvironment()
  asyncio.run(env.initialize())
  sdk.created[0].state = "deleted"
  result = asyncio.run(env.execute("echo hi"))
  assert result.exit_code == 0
  assert len(sdk.created) == 2 and env.sandbox_id == sdk.created[1].id
  asyncio.run(env.close())
  assert sdk.created[1].deleted


async def test_parallel_calls_replace_a_gone_sandbox_once(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  sdk.created[0].state = "deleted"
  results = await asyncio.gather(*(env.execute(f"echo {i}") for i in range(5)))
  assert all(r.exit_code == 0 for r in results)
  assert len(sdk.created) == 2


async def test_a_failed_replacement_is_retried_on_the_next_call(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  sdk.created[0].state = "deleted"
  sdk.fail_creates = 1
  with pytest.raises(PrizedError):
    await env.execute("echo 1")
  assert (await env.execute("echo 2")).exit_code == 0
  assert len(sdk.created) == 2


async def test_a_sandbox_in_error_is_deleted_when_replaced(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  failed = sdk.created[0]
  failed.state = "error"

  def refuse(cmd, kwargs):
    raise PrizedError("sandbox_not_running", status=409)

  failed.handler = only("echo", refuse)
  assert (await env.execute("echo hi")).exit_code == 0
  assert failed.deleted and len(sdk.created) == 2


async def test_gone_attached_sandbox_is_not_replaced(sdk):
  env = PrizedEnvironment(on_close="keep")
  await env.initialize()
  attached = PrizedEnvironment(sandbox_id=env.sandbox_id)
  await attached.initialize()
  sdk.created[0].state = "deleted"
  with pytest.raises(RuntimeError, match="gone"):
    await attached.execute("echo hi")
  assert len(sdk.created) == 1


async def test_other_errors_are_raised(sdk):
  env = PrizedEnvironment()
  await env.initialize()

  def unreachable(cmd, kwargs):
    raise PrizedError("sandbox_unreachable", status=502)

  sdk.created[0].handler = only("echo", unreachable)
  with pytest.raises(PrizedError) as info:
    await env.execute("echo hi")
  assert info.value.code == "sandbox_unreachable"
  assert len(sdk.created) == 1


# ---- fork, pause, loops, the toolset -----------------------------------------


async def test_fork_returns_initialized_copies_deleted_on_close(sdk):
  env = PrizedEnvironment(on_close="keep", template="prized-agent-v1")
  await env.initialize()
  await env.write_file("state.txt", "1")

  copies = await env.fork(2)

  assert len(copies) == 2
  for copy in copies:
    assert copy._is_initialized
    assert copy.sandbox_id != env.sandbox_id
    assert copy._template == "prized-agent-v1"  # a replacement matches
    assert await copy.read_file("state.txt") == b"1"
    await copy.close()
  assert all(k.deleted for k in sdk.created[0].kids)
  (kept,) = await env.fork(1, on_close="keep")
  await kept.close()
  assert not sdk.created[0].kids[-1].deleted


async def test_pause(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  await env.pause()
  assert sdk.created[0].state == "paused"


def test_new_event_loops_reconnect(sdk):
  """ADK's sync Runner.run starts a new event loop per call."""
  env = PrizedEnvironment()
  asyncio.run(env.initialize())
  asyncio.run(env.execute("echo 1"))
  asyncio.run(env.execute("echo 2"))
  asyncio.run(env.close())  # closing reconnects too, then deletes
  assert len(sdk.created) == 1
  assert len(sdk.connects) == 3
  assert sdk.created[0].deleted


def test_parallel_calls_on_a_new_loop_reconnect_once(sdk):
  env = PrizedEnvironment()
  asyncio.run(env.initialize())

  async def burst():
    await asyncio.gather(*(env.execute("true") for _ in range(5)))

  asyncio.run(burst())
  assert len(sdk.connects) == 1


async def test_environment_toolset_drives_the_sandbox(sdk):
  env = PrizedEnvironment()
  toolset = EnvironmentToolset(environment=env)
  tools = {tool.name: tool for tool in await toolset.get_tools()}
  assert set(tools) >= {"Execute", "ReadFile", "EditFile", "WriteFile"}
  box = sdk.created[0]
  context = mock.MagicMock()

  await tools["WriteFile"].run_async(
      args={"path": "app.py", "content": "print('a')\n"},
      tool_context=context,
  )
  assert box.fs["/home/prized/work/app.py"] == b"print('a')\n"
  edited = await tools["EditFile"].run_async(
      args={"path": "app.py", "old_string": "'a'", "new_string": "'b'"},
      tool_context=context,
  )
  assert edited["status"] == "ok", edited
  assert box.fs["/home/prized/work/app.py"] == b"print('b')\n"
  box.handler = only("app.py", lambda cmd, kw: ExecResult("b\n", "", 0))
  ran = await tools["Execute"].run_async(
      args={"command": "python3 app.py"}, tool_context=context
  )
  assert ran == {"status": "ok", "stdout": "b\n"}
  missing = await tools["EditFile"].run_async(
      args={"path": "nope.py", "old_string": "a", "new_string": "b"},
      tool_context=context,
  )
  assert missing["status"] == "error"

  await toolset.close()
  assert box.deleted


# ---- threads, close races, and the remaining gone paths -----------------------


def _threads(*targets):
  """Runs each target on its own thread (each with its own event loop)."""
  import threading

  errors = []

  def wrap(target):
    try:
      target()
    except Exception as e:  # pylint: disable=broad-except
      errors.append(e)

  threads = [threading.Thread(target=wrap, args=(t,)) for t in targets]
  for t in threads:
    t.start()
  for t in threads:
    t.join()
  return errors


def test_two_threads_initializing_keep_one_sandbox(sdk):
  """Concurrent sync Runner.run calls on a shared toolset (ADK supports it)."""
  sdk.create_delay = 0.05
  env = PrizedEnvironment()
  errors = _threads(
      lambda: asyncio.run(env.initialize()),
      lambda: asyncio.run(env.initialize()),
  )
  assert not errors
  assert len(sdk.alive()) == 1  # a second create, if any, was deleted
  assert env.sandbox_id == sdk.alive()[0].id
  asyncio.run(env.close())
  assert not sdk.alive()


def test_two_threads_replace_a_gone_sandbox_once(sdk):
  env = PrizedEnvironment()
  asyncio.run(env.initialize())
  sdk.created[0].state = "deleted"
  sdk.create_delay = 0.05
  results = []
  errors = _threads(
      lambda: results.append(asyncio.run(env.execute("echo a"))),
      lambda: results.append(asyncio.run(env.execute("echo b"))),
  )
  assert not errors, errors
  assert [r.exit_code for r in results] == [0, 0]
  assert len(sdk.alive()) == 1
  asyncio.run(env.close())
  assert not sdk.alive()


def test_threads_each_use_their_own_handle(sdk):
  """A handle belongs to its loop: no thread may get another loop's handle."""
  env = PrizedEnvironment()
  asyncio.run(env.initialize())

  def burst():
    async def go():
      await asyncio.gather(*(env.execute("true") for _ in range(3)))

    asyncio.run(go())

  assert not _threads(burst, burst, burst)  # the fakes refuse cross-loop use
  assert len(sdk.created) == 1


async def test_a_call_in_flight_during_close_makes_no_new_sandbox(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  box = sdk.created[0]
  started = asyncio.Event()

  async def slow(cmd, kwargs):
    if cmd != "long":
      return None
    started.set()
    await asyncio.sleep(0.05)
    box.check_alive()  # deleted meanwhile: not_found

  box.handler = slow
  call = asyncio.ensure_future(env.execute("long"))
  await started.wait()
  await env.close()
  with pytest.raises(RuntimeError, match="not started"):
    await call
  assert len(sdk.created) == 1 and box.deleted


def test_close_on_a_new_loop_deletes_a_failed_sandbox(sdk):
  env = PrizedEnvironment()
  asyncio.run(env.initialize())
  sdk.created[0].state = "error"
  asyncio.run(env.close())
  assert sdk.created[0].deleted
  assert len(sdk.created) == 1


async def test_resuming_a_failed_sandbox_deletes_it(sdk):
  env = PrizedEnvironment(on_close="pause")
  await env.initialize()
  await env.close()
  sdk.created[0].state = "error"
  await env.initialize()
  assert sdk.created[0].deleted
  assert len(sdk.alive()) == 1


async def test_close_pauses_without_waiting(sdk):
  env = PrizedEnvironment(on_close="pause")
  await env.initialize()
  await env.close()
  assert sdk.created[0].pause_kwargs == [{"wait": False}]


async def test_path_is_read_once_per_sandbox(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  for _ in range(3):
    await env.execute("true")
  calls = [c for c, _ in sdk.created[0].calls if c == ["printenv", "PATH"]]
  assert len(calls) == 1


async def test_permission_denied_reads_go_through_the_shell(sdk):
  env = PrizedEnvironment()
  await env.initialize()
  box = sdk.created[0]
  box.fs["/home/prized/work/locked.txt"] = b"x"
  real_read = box.handles[0].files.read_bytes

  async def refuse(path):
    raise PrizedError("permission_denied", status=403)

  box.handles[0].files.read_bytes = refuse
  assert await env.read_file("locked.txt") == b"x"
  assert box.calls[-1][0] == [
      "base64",
      "-w0",
      "--",
      "/home/prized/work/locked.txt",
  ]
  box.handles[0].files.read_bytes = real_read


async def test_fork_copies_inherit_the_create_options(sdk):
  env = PrizedEnvironment(vcpu=1, memory_gb=2, labels={"team": "x"})
  await env.initialize()
  (copy,) = await env.fork(1)
  assert copy._create_kwargs == env._create_kwargs
  await copy.close()
