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

"""Code executor that runs model-written code in a Prized sandbox."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import logging
import mimetypes
import posixpath
import shlex
import threading
from typing import Any, Optional, TYPE_CHECKING
import uuid

from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.code_executors.code_execution_utils import CodeExecutionInput
from google.adk.code_executors.code_execution_utils import CodeExecutionResult
from google.adk.code_executors.code_execution_utils import File
from pydantic import Field
from pydantic import PrivateAttr
from typing_extensions import override

if TYPE_CHECKING:
  from google.adk.agents.invocation_context import InvocationContext
  from prized import Sandbox

logger = logging.getLogger("google_adk." + __name__)

_WORKDIR = "/home/prized/work"
_SCRATCH = "/home/prized/.prized/adk"
_TIMEOUT_EXIT_CODE = -1
_MAX_OUTPUT_FILES = 10
_MAX_OUTPUT_FILE_BYTES = 20 * 1024 * 1024
_LABELS = {"adk": "code-executor"}
# Lets code that shells out to `pip install` install for the user (no root).
_DEFAULT_ENV = {"PIP_BREAK_SYSTEM_PACKAGES": "1"}
_MAYBE_GONE_CODES = frozenset(
    {"not_found", "sandbox_not_running", "sandbox_deleted"}
)
# Every sandbox the executor makes pauses when idle and is deleted an hour
# after that, so one that nothing deletes does not outlive its use by much.
_IDLE_PAUSE_MIN = 5
_PAUSED_DELETE_MIN = 60
_HAS_EXIT_CODE = "exit_code" in {
    f.name for f in dataclasses.fields(CodeExecutionResult)
}


class PrizedCodeExecutor(BaseCodeExecutor):
  """Runs the code blocks an agent writes in a Prized sandbox.

  By default every execution gets a fresh sandbox (a Firecracker microVM)
  that is deleted when the code ends. With
  ``keep_sandbox=True`` one sandbox serves every execution of this executor,
  so files and installed packages carry over (Python variables do not); call
  ``close()`` to delete it. ``sandbox_id`` runs the code in an existing
  sandbox, which is never deleted.

  Input files are written to the working directory, and files the code
  creates there come back as output files (saved as artifacts by the agent).
  The code runs on stdin with the working directory as its first import
  path. ``Runner.close()`` does not close code executors: call ``close()``
  yourself with ``keep_sandbox=True``.

  Requires a Prized API key: ``PRIZED_API_KEY`` or ``api_key=``.
  """

  template: str = "prized-base-v1"
  """``prized-base-v1`` (Python 3.12, Node and git) or ``prized-agent-v1``."""

  sandbox_id: Optional[str] = None
  """Run in this existing sandbox instead of creating one. It is kept."""

  keep_sandbox: bool = False
  """Reuse one sandbox for every execution instead of one each."""

  vcpu: Optional[int] = None
  memory_gb: Optional[float] = None
  env_vars: Optional[dict[str, str]] = None
  """Set for every command in a new sandbox. The model's code can read them."""

  labels: Optional[dict[str, str]] = None
  """Labels for a new sandbox, to find it later."""

  python: str = "python3"
  """The interpreter. The code reaches it on stdin, in the working directory."""

  api_key: Optional[str] = Field(default=None, repr=False, exclude=True)
  """Prized API key. If ``None``, ``PRIZED_API_KEY`` is used."""

  base_url: Optional[str] = None

  # Overrides the BaseCodeExecutor attribute: Python state does not persist
  # between executions (files do, with keep_sandbox or sandbox_id).
  stateful: bool = Field(default=False, frozen=True, exclude=True)

  # Overrides the BaseCodeExecutor attribute: the base default of None waits
  # forever, which non-terminating generated code turns into a hung agent.
  timeout_seconds: Optional[int] = 300
  """Wall-clock limit for one execution, in seconds. None waits forever."""

  _sandbox: Optional[Sandbox] = PrivateAttr(default=None)
  _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

  def __init__(self, **data: Any):
    if data.get("stateful"):
      raise ValueError("Cannot set `stateful=True` in PrizedCodeExecutor.")
    if data.get("sandbox_id") and data.get("keep_sandbox"):
      raise ValueError("Use either sandbox_id or keep_sandbox, not both.")
    super().__init__(**data)

  @override
  def execute_code(
      self,
      invocation_context: InvocationContext,
      code_execution_input: CodeExecutionInput,
  ) -> CodeExecutionResult:
    from prized import PrizedError

    sandbox: Optional[Sandbox] = None
    temporary = False
    try:
      sandbox, temporary = self._acquire()
      try:
        return self._run(sandbox, code_execution_input)
      except PrizedError as e:
        if temporary or not self.keep_sandbox:
          raise
        if e.code not in _MAYBE_GONE_CODES or not self._forget(sandbox):
          raise
        # The kept sandbox is gone (deleted after pausing, or its run time
        # ended): run in a fresh one, which is kept from now on.
        logger.warning(
            "Kept Prized sandbox %s is gone; starting a new one.", sandbox.id
        )
        sandbox, temporary = self._acquire()
        return self._run(sandbox, code_execution_input)
    except PrizedError as e:
      logger.error("Prized sandbox error: %s", e)
      return _result(stderr=f"Prized sandbox error: {e}")
    finally:
      if sandbox is not None and temporary:
        _discard(sandbox)

  def close(self) -> None:
    """Delete the kept sandbox, if there is one. A ``sandbox_id`` one stays."""
    with self._lock:
      sandbox, self._sandbox = self._sandbox, None
    if sandbox is not None and self.keep_sandbox:
      _discard(sandbox)
    elif sandbox is not None:
      sandbox.close()

  def _forget(self, sandbox: Sandbox) -> bool:
    """Drops the kept sandbox if it is gone; True when it was."""
    from prized import PrizedError

    try:
      sandbox.refresh()
      gone = sandbox.state in ("deleting", "deleted", "error")
    except PrizedError as e:
      gone = e.code == "not_found"
    if gone:
      with self._lock:
        if self._sandbox is sandbox:
          self._sandbox = None
      if sandbox.state == "error":
        _discard(sandbox)  # a failed sandbox still counts toward quota
      else:
        with contextlib.suppress(Exception):
          sandbox.close()
    return gone

  def _acquire(self) -> tuple[Sandbox, bool]:
    """The sandbox for one execution, and whether to delete it afterwards."""
    from prized import Sandbox

    if not self.keep_sandbox and not self.sandbox_id:
      return self._create(), True
    with self._lock:
      if self._sandbox is None:
        if self.sandbox_id:
          self._sandbox = Sandbox.connect(
              self.sandbox_id, api_key=self.api_key, base_url=self.base_url
          )
        else:
          self._sandbox = self._create()
      return self._sandbox, False

  def _create(self) -> Sandbox:
    from prized import Sandbox

    return Sandbox.create(
        self.template,
        vcpu=self.vcpu,
        memory_gb=self.memory_gb,
        env_vars={**_DEFAULT_ENV, **(self.env_vars or {})},
        labels={**_LABELS, **(self.labels or {})},
        # A command keeps its sandbox awake while it runs, so these only
        # reclaim a sandbox nothing deleted (a crashed process, no close()).
        auto_pause_min=_IDLE_PAUSE_MIN,
        auto_delete_min=_PAUSED_DELETE_MIN,
        api_key=self.api_key,
        base_url=self.base_url,
    )

  def _run(
      self, sandbox: Sandbox, code_execution_input: CodeExecutionInput
  ) -> CodeExecutionResult:
    from prized import CommandTimeoutError

    run_id = uuid.uuid4().hex
    script = f"{_SCRATCH}/{run_id}.py"
    marker = f"{_SCRATCH}/{run_id}.start"
    for file in code_execution_input.input_files:
      sandbox.files.write(_input_path(file.name), _as_bytes(file.content))
    sandbox.files.write(script, code_execution_input.code)
    # Files the code writes are newer than this marker.
    sandbox.files.write(marker, "")
    timed_out = False
    try:
      # On stdin, so the working directory is the code's first import path;
      # pip-installed commands (~/.local/bin) are on its PATH.
      result = sandbox.exec(
          'PATH="$HOME/.local/bin:$PATH"'
          f" {shlex.quote(self.python)} < {shlex.quote(script)}",
          cwd=_WORKDIR,
          env=_DEFAULT_ENV,
          timeout=self.timeout_seconds or 0,
      )
      stdout, stderr = result.stdout, result.stderr
      exit_code = result.exit_code
    except CommandTimeoutError as e:
      timed_out = True
      partial = e.result
      stdout = partial.stdout if partial else ""
      stderr = partial.stderr if partial else ""
      exit_code = _TIMEOUT_EXIT_CODE
    if timed_out:
      note = f"Code execution timed out after {self.timeout_seconds} seconds."
      stderr = f"{stderr}\n{note}" if stderr else note
    output_files = _collect_outputs(sandbox, marker)
    sandbox.exec(["rm", "-f", "--", script, marker], timeout=30)
    return _result(
        stdout=stdout,
        stderr=stderr,
        output_files=output_files,
        exit_code=exit_code,
    )


def _collect_outputs(sandbox: Sandbox, marker: str) -> list[File]:
  """Files in the working directory written since ``marker``."""
  listing = sandbox.exec(
      [
          "find",
          _WORKDIR,
          "-maxdepth",
          "1",
          "-type",
          "f",
          "-newer",
          marker,
          "-printf",
          "%s\\t%f\\n",
      ],
      timeout=30,
  )
  files: list[File] = []
  for line in sorted(listing.stdout.splitlines()):
    size_text, _, name = line.partition("\t")
    if not name or not size_text.isdigit():
      continue
    if int(size_text) > _MAX_OUTPUT_FILE_BYTES:
      logger.warning("Skipping output file %s: larger than 20 MiB.", name)
      continue
    if len(files) == _MAX_OUTPUT_FILES:
      logger.warning("Returning the first %d output files.", _MAX_OUTPUT_FILES)
      break
    data = sandbox.files.read_bytes(posixpath.join(_WORKDIR, name))
    mime_type, _ = mimetypes.guess_type(name)
    files.append(
        File(
            name=name,
            content=base64.b64encode(data).decode("ascii"),
            mime_type=mime_type or "application/octet-stream",
        )
    )
  return files


def _input_path(name: str) -> str:
  base = posixpath.basename(name)
  if base in ("", ".", ".."):
    raise ValueError(f"Bad input file name: {name!r}")
  return posixpath.join(_WORKDIR, base)


def _as_bytes(content: str | bytes) -> bytes:
  # ADK passes input files base64-encoded as str, raw as bytes.
  if isinstance(content, bytes):
    return content
  return base64.b64decode(content)


def _result(
    *,
    stdout: str = "",
    stderr: str = "",
    output_files: Optional[list[File]] = None,
    exit_code: Optional[int] = None,
) -> CodeExecutionResult:
  result = CodeExecutionResult(
      stdout=stdout, stderr=stderr, output_files=output_files or []
  )
  if _HAS_EXIT_CODE:
    setattr(result, "exit_code", exit_code)
  return result


def _discard(sandbox: Sandbox) -> None:
  try:
    sandbox.delete()
  except Exception:  # pylint: disable=broad-except
    logger.warning("Could not delete Prized sandbox %s", sandbox.id)
  finally:
    sandbox.close()
