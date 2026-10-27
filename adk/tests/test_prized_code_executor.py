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

"""Tests for PrizedCodeExecutor (no network: the SDK is faked)."""

import base64
import dataclasses
from typing import Any, Optional
from unittest import mock

from google.adk.code_executors.code_execution_utils import CodeExecutionInput
from google.adk.code_executors.code_execution_utils import CodeExecutionResult
from google.adk.code_executors.code_execution_utils import File
import prized
from prized import CommandTimeoutError
from prized import ExecResult
from prized import PrizedError
import pytest

from prized_adk import PrizedCodeExecutor

_HAS_EXIT_CODE = "exit_code" in {
    f.name for f in dataclasses.fields(CodeExecutionResult)
}


class FakeSandbox:
  """A sync sandbox: files in ``fs``; ``run`` decides what python prints."""

  count = 0

  def __init__(self, **kwargs: Any):
    FakeSandbox.count += 1
    self.id = f"sbx_sync{FakeSandbox.count}"
    self.kwargs = kwargs
    self.fs: dict[str, bytes] = {}
    self.state = "running"
    self.closed = False
    self.commands: list[Any] = []
    self.run = lambda box, cmd, kw: ExecResult("", "", 0)
    self.files = mock.MagicMock()
    self.files.write.side_effect = self._write
    self.files.read_bytes.side_effect = lambda path: self.fs[path]
    self.marker_mtime: dict[str, int] = {}
    self.clock = 0

  def _write(self, path: str, data: Any):
    self.clock += 1
    self.fs[path] = data.encode() if isinstance(data, str) else data
    self.marker_mtime[path] = self.clock

  def exec(self, cmd: Any, **kwargs: Any) -> ExecResult:
    if self.state == "deleted":
      raise PrizedError("not_found", status=404, sandbox_id=self.id)
    self.commands.append((cmd, kwargs))
    if isinstance(cmd, str):  # the code run: PATH=... python3 < script
      return self.run(self, cmd, kwargs)
    if cmd[0] == "find":
      marker = cmd[cmd.index("-newer") + 1]
      since = self.marker_mtime[marker]
      lines = [
          f"{len(data)}\t{path.rsplit('/', 1)[1]}"
          for path, data in self.fs.items()
          if path.startswith("/home/prized/work/")
          and path.count("/") == 4
          and self.marker_mtime.get(path, 0) > since
      ]
      return ExecResult("\n".join(lines) + "\n", "", 0)
    if cmd[0] == "rm":
      for path in cmd[3:]:
        self.fs.pop(path, None)
      return ExecResult("", "", 0)
    return self.run(self, cmd, kwargs)

  def delete(self, **_: Any) -> None:
    self.state = "deleted"

  def refresh(self) -> "FakeSandbox":
    if self.state == "deleted":
      raise PrizedError("not_found", status=404, sandbox_id=self.id)
    return self

  def close(self) -> None:
    self.closed = True


def script_of(cmd: str) -> str:
  """The staged script a code run reads on stdin."""
  return cmd.rsplit(" < ", 1)[1]


def code_runs(box: "FakeSandbox") -> list:
  return [(c, kw) for c, kw in box.commands if isinstance(c, str)]


class Sdk:

  def __init__(self) -> None:
    self.created: list[FakeSandbox] = []
    self.next_run = None

  def create(self, template: str = "prized-base-v1", **kwargs: Any):
    box = FakeSandbox(template=template, **kwargs)
    if self.next_run is not None:
      box.run = self.next_run
    self.created.append(box)
    return box

  def connect(self, sandbox_id: str, **_: Any):
    box = FakeSandbox()
    box.id = sandbox_id
    if self.next_run is not None:
      box.run = self.next_run
    self.created.append(box)
    return box


@pytest.fixture(name="sdk")
def _sdk():
  sdk = Sdk()
  with (
      mock.patch.object(
          prized.Sandbox, "create", new=mock.MagicMock(side_effect=sdk.create)
      ),
      mock.patch.object(
          prized.Sandbox, "connect", new=mock.MagicMock(side_effect=sdk.connect)
      ),
  ):
    yield sdk


def _run(executor: PrizedCodeExecutor, code: str, files: Optional[list] = None):
  return executor.execute_code(
      mock.MagicMock(), CodeExecutionInput(code=code, input_files=files or [])
  )


def test_each_execution_gets_a_fresh_sandbox_that_is_deleted(sdk):
  sdk.next_run = lambda box, cmd, kw: ExecResult("42\n", "", 0)
  executor = PrizedCodeExecutor()

  first = _run(executor, "print(42)")
  second = _run(executor, "print(42)")

  assert first.stdout == "42\n" and first.stderr == ""
  assert second.stdout == "42\n"
  assert len(sdk.created) == 2
  for box in sdk.created:
    assert box.state == "deleted" and box.closed
    assert box.kwargs["labels"] == {"adk": "code-executor"}
    # Leak guards for a sandbox nothing deletes (a crashed process).
    assert box.kwargs["auto_pause_min"] == 5
    assert box.kwargs["auto_delete_min"] == 60
    ((cmd, kwargs),) = code_runs(box)
    # The code goes in on stdin, so the working directory is on its import
    # path, and pip-installed commands are on PATH.
    assert cmd.startswith('PATH="$HOME/.local/bin:$PATH" python3 < ')
    assert script_of(cmd).startswith("/home/prized/.prized/adk/")
    assert kwargs == {
        "cwd": "/home/prized/work",
        "env": {"PIP_BREAK_SYSTEM_PACKAGES": "1"},
        "timeout": 300,
    }
  if _HAS_EXIT_CODE:
    assert first.exit_code == 0


def test_code_is_written_to_the_script(sdk):
  seen = {}

  def run(box, cmd, kw):
    seen["code"] = box.fs[script_of(cmd)]
    return ExecResult("", "", 0)

  sdk.next_run = run
  _run(PrizedCodeExecutor(), "print('hi')")
  assert seen["code"] == b"print('hi')"


def test_keep_sandbox_reuses_one_until_close(sdk):
  executor = PrizedCodeExecutor(keep_sandbox=True)
  _run(executor, "a = 1")
  _run(executor, "b = 2")
  assert len(sdk.created) == 1
  box = sdk.created[0]
  assert box.kwargs["auto_pause_min"] == 5
  assert box.kwargs["auto_delete_min"] == 60
  assert box.state == "running"
  executor.close()
  assert box.state == "deleted"


def test_kept_sandbox_that_is_gone_is_replaced(sdk):
  """Deleted after pausing (or its run time ended): the next run starts anew."""
  sdk.next_run = lambda box, cmd, kw: ExecResult("ok\n", "", 0)
  executor = PrizedCodeExecutor(keep_sandbox=True, labels={"team": "x"})
  _run(executor, "a = 1")
  first = sdk.created[0]
  assert first.kwargs["labels"] == {"adk": "code-executor", "team": "x"}
  first.state = "deleted"
  result = _run(executor, "b = 2")
  assert result.stdout == "ok\n"
  assert len(sdk.created) == 2
  _run(executor, "c = 3")
  assert len(sdk.created) == 2  # the new one is kept
  executor.close()
  assert sdk.created[1].state == "deleted"


def test_sandbox_id_is_used_and_kept(sdk):
  executor = PrizedCodeExecutor(sandbox_id="sbx_mine")
  _run(executor, "print(1)")
  _run(executor, "print(2)")
  assert [b.id for b in sdk.created] == ["sbx_mine"]
  executor.close()
  assert sdk.created[0].state == "running"
  assert sdk.created[0].closed


def test_timeout_reports_partial_output(sdk):
  def slow(box, cmd, kw):
    raise CommandTimeoutError(ExecResult("start\n", "", -1, timed_out=True), 5)

  sdk.next_run = slow
  result = _run(PrizedCodeExecutor(timeout_seconds=5), "while True: pass")
  assert result.stdout == "start\n"
  assert "timed out after 5 seconds" in result.stderr
  if _HAS_EXIT_CODE:
    assert result.exit_code == -1
  assert sdk.created[0].state == "deleted"


def test_cut_output_says_so(sdk):
  sdk.next_run = lambda box, cmd, kw: ExecResult("1\n", "", 0, truncated=True)
  result = _run(PrizedCodeExecutor(), "print(1)")
  assert result.stdout == "1\n"
  assert "kept only the start of this output" in result.stderr


def test_no_timeout_means_no_limit(sdk):
  _run(PrizedCodeExecutor(timeout_seconds=None), "print(1)")
  ((_, kwargs),) = code_runs(sdk.created[0])
  assert kwargs["timeout"] == 0


def test_input_files_are_written_and_output_files_returned(sdk):
  def run(box, cmd, kw):
    box._write("/home/prized/work/plot.png", b"\x89PNG")
    return ExecResult("saved\n", "", 0)

  sdk.next_run = run
  csv = File(
      name="data_1_1.csv",
      content=base64.b64encode(b"a,b\n1,2\n").decode(),
      mime_type="text/csv",
  )
  result = _run(PrizedCodeExecutor(), "plot()", files=[csv])

  box = sdk.created[0]
  assert box.fs["/home/prized/work/data_1_1.csv"] == b"a,b\n1,2\n"
  assert [f.name for f in result.output_files] == ["plot.png"]
  (png,) = result.output_files
  assert png.mime_type == "image/png"
  assert base64.b64decode(png.content) == b"\x89PNG"


def test_sandbox_errors_become_stderr(sdk):
  def broken(box, cmd, kw):
    raise PrizedError("sandbox_unreachable", status=502)

  sdk.next_run = broken
  result = _run(PrizedCodeExecutor(), "print(1)")
  assert "Prized sandbox error" in result.stderr
  assert sdk.created[0].state == "deleted"


def test_create_failure_becomes_stderr():
  with mock.patch(
      "prized.Sandbox.create",
      side_effect=PrizedError("sandbox_quota_exceeded", status=429),
  ):
    result = _run(PrizedCodeExecutor(), "print(1)")
  assert "sandbox_quota_exceeded" in result.stderr or "quota" in result.stderr


@pytest.mark.parametrize(
    "kwargs",
    [{"stateful": True}, {"sandbox_id": "sbx_1", "keep_sandbox": True}],
)
def test_invalid_options_raise(kwargs):
  with pytest.raises(ValueError):
    PrizedCodeExecutor(**kwargs)


def test_api_key_is_not_serialized():
  executor = PrizedCodeExecutor(api_key="dcp_secret")
  assert "dcp_secret" not in repr(executor)
  assert "api_key" not in executor.model_dump()


def test_kept_sandbox_in_error_is_deleted_and_replaced(sdk):
  sdk.next_run = lambda box, cmd, kw: (
      ExecResult("ok\n", "", 0) if box.state == "running" else None
  )
  executor = PrizedCodeExecutor(keep_sandbox=True)
  _run(executor, "a = 1")
  failed = sdk.created[0]
  failed.state = "error"
  original = failed.run

  def refuse(box, cmd, kw):
    raise PrizedError("sandbox_not_running", status=409)

  failed.run = refuse
  result = _run(executor, "b = 2")
  assert result.stdout == "ok\n"
  assert failed.state == "deleted"  # a failed sandbox counts toward quota
  assert len(sdk.created) == 2
  failed.run = original
  executor.close()
