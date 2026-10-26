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

"""Prized sandbox code execution environment."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import logging
import os
from pathlib import Path
import posixpath
import threading
from typing import Any, Literal, Optional, TYPE_CHECKING
import uuid

from google.adk.environment import BaseEnvironment
from google.adk.environment import ExecutionResult
from typing_extensions import override

if TYPE_CHECKING:
  from prized import AsyncSandbox
  from prized import ExecResult
  from prized import PrizedError

logger = logging.getLogger("google_adk." + __name__)

_DEFAULT_TEMPLATE = "prized-base-v1"
_HOME = "/home/prized"
_DEFAULT_WORKING_DIR = "/home/prized/work"
# File writes go through a staging file here, then into place with the shell.
_STAGING_DIR = "/home/prized/.prized/adk"
_DEFAULT_AUTO_PAUSE_MIN = 10
# A sandbox that close() deletes is also deleted this long after it pauses, in
# case nothing closes it.
_DEFAULT_AUTO_DELETE_MIN = 60
_UNSET: Any = object()
_TIMEOUT_EXIT_CODE = -1
# A helper command that reads or writes a file through the shell.
_HELPER_TIMEOUT = 120
_GONE_STATES = frozenset({"deleting", "deleted", "error"})
# Errors after which the sandbox may be gone (deleted at the end of its run
# time, or by someone else).
_MAYBE_GONE_CODES = frozenset(
    {"not_found", "sandbox_not_running", "sandbox_deleted"}
)
# File API refusals the shell can still serve: it follows symlinks, reaches
# outside /home/prized, and reports the errno.
_SHELL_FALLBACK_CODES = frozenset(
    {"symlink_not_allowed", "permission_denied", "path_outside_root"}
)
_LABELS = {"adk": "environment"}
_LOCAL_BIN = "/home/prized/.local/bin"
_FALLBACK_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
# The templates' Python is "externally managed": without this, the model's
# `pip install` fails. With it, pip installs for the user (no root needed) into
# ~/.local, whose bin directory every command also gets on its PATH.
_PIP_ENV = {"PIP_BREAK_SYSTEM_PACKAGES": "1"}
_REPLACED_NOTE = (
    "[prized: the previous sandbox was gone (its run time ended or it was"
    " deleted after being paused), so this ran in a fresh, empty sandbox:"
    " earlier files and installs are not there]\n"
)
_NOT_STARTED = "Sandbox is not started. Call initialize() first."

OnClose = Literal["delete", "pause", "keep"]


class PrizedEnvironment(BaseEnvironment):
  """A persistent remote workspace backed by a Prized sandbox.

  A Prized sandbox is a Firecracker microVM (Ubuntu with Python, Node and git)
  that pauses with its memory and processes, resumes where it stopped, and
  forks into live copies.

  ``initialize()`` creates a sandbox, or attaches to ``sandbox_id``.
  ``close()`` deletes a sandbox this environment created and leaves an
  attached one as it is; ``on_close`` changes that (``"pause"`` keeps the
  sandbox, memory and disk, for a later ``PrizedEnvironment(sandbox_id=...)``,
  or for this environment's next ``initialize()``).

  An idle sandbox pauses itself after ``auto_pause_min`` minutes (no compute
  is charged while paused), and the next command resumes it with every file,
  install and process intact. If a sandbox this environment created is gone
  (its run time ended, or it was deleted after pausing), the next operation
  creates a fresh one, logs a warning and says so in the next command's
  output: files and installs from before are lost.

  One environment may be used from several threads and event loops at once
  (ADK's sync ``Runner.run`` runs each call on a new loop): each loop gets its
  own connection to the same sandbox.

  Requires a Prized API key: ``PRIZED_API_KEY`` or ``api_key=``.
  """

  def __init__(
      self,
      *,
      template: Optional[str] = None,
      sandbox_id: Optional[str] = None,
      vcpu: Optional[int] = None,
      memory_gb: Optional[float] = None,
      disk_gb: Optional[int] = None,
      env_vars: Optional[dict[str, str]] = None,
      labels: Optional[dict[str, str]] = None,
      working_dir: str = _DEFAULT_WORKING_DIR,
      auto_pause_min: Optional[int] = _UNSET,
      auto_delete_min: Optional[int] = _UNSET,
      max_runtime_min: Optional[int] = _UNSET,
      on_close: Optional[OnClose] = None,
      api_key: Optional[str] = None,
      base_url: Optional[str] = None,
  ):
    """Create a Prized environment.

    Args:
      template: Sandbox template. ``prized-base-v1`` (the default: Ubuntu with
        Node, Python 3.12 and git) or ``prized-agent-v1`` (adds Claude Code and
        Codex).
      sandbox_id: An existing sandbox (``sbx_...``) to attach to instead of
        creating one. A paused one resumes on the first command.
      vcpu: vCPUs for a new sandbox. Defaults to the template's.
      memory_gb: Memory for a new sandbox, in GB. Defaults to the template's.
      disk_gb: Disk for a new sandbox, in GB. Defaults to the template's.
      env_vars: Environment variables set for every command in a new sandbox.
        Code in the sandbox, the model's included, can read them.
      labels: Labels for a new sandbox, to find it later.
      working_dir: Absolute directory where commands start and relative paths
        resolve. Created if missing. Defaults to ``/home/prized/work``.
      auto_pause_min: Idle minutes before a new sandbox pauses itself. ``0``
        turns it off. Defaults to 10.
      auto_delete_min: Minutes after pausing before a new sandbox is deleted.
        Defaults to 60 when ``close()`` would delete it (so a sandbox nobody
        closes goes away) and to never otherwise. ``0`` turns it off.
      max_runtime_min: Minutes of running before a new sandbox is deleted.
        Defaults to the workspace's (24 hours on a paid workspace, 60 on the
        free tier).
      on_close: What ``close()`` does: ``"delete"``, ``"pause"`` or
        ``"keep"``. Defaults to ``"delete"`` for a sandbox this environment
        created and ``"keep"`` for one it attached to.
      api_key: Prized API key. If ``None``, ``PRIZED_API_KEY`` is used.
      base_url: Prized API URL. If ``None``, ``https://api.prized.dev``.

    Raises:
      ValueError: If the options are invalid or contradict each other.
    """
    create_options = (template, vcpu, memory_gb, disk_gb, env_vars, labels)
    lifetimes = (auto_pause_min, auto_delete_min, max_runtime_min)
    if sandbox_id is not None and (
        any(option is not None for option in create_options)
        or any(option is not _UNSET for option in lifetimes)
    ):
      raise ValueError(
          "template, vcpu, memory_gb, disk_gb, env_vars, labels,"
          " auto_pause_min, auto_delete_min and max_runtime_min apply to a new"
          " sandbox; they cannot be used with sandbox_id"
      )
    if on_close not in (None, "delete", "pause", "keep"):
      raise ValueError("on_close must be 'delete', 'pause' or 'keep'")
    if not posixpath.isabs(working_dir):
      raise ValueError("working_dir must be an absolute path")
    if auto_pause_min is _UNSET:
      auto_pause_min = _DEFAULT_AUTO_PAUSE_MIN
    if auto_delete_min is _UNSET:
      auto_delete_min = (
          _DEFAULT_AUTO_DELETE_MIN if on_close in (None, "delete") else None
      )
    if max_runtime_min is _UNSET:
      max_runtime_min = None

    self._template = template or _DEFAULT_TEMPLATE
    self._requested_sandbox_id = sandbox_id
    self._create_kwargs: dict[str, Any] = {
        "vcpu": vcpu,
        "memory_gb": memory_gb,
        "disk_gb": disk_gb,
        "env_vars": {**_PIP_ENV, **(env_vars or {})},
        "labels": {**_LABELS, **(labels or {})},
        "auto_pause_min": auto_pause_min,
        "auto_delete_min": auto_delete_min,
        "max_runtime_min": max_runtime_min,
    }
    self._working_dir = posixpath.normpath(working_dir)
    self._on_close = on_close
    self._api_key = api_key
    self._base_url = base_url
    # Which sandbox is in use is shared by every thread and event loop, under
    # `_state`. Each loop keeps its own handle (the SDK's connections belong
    # to the loop that opened them), valid for one generation: a replacement
    # or a close starts the next.
    self._state = threading.Lock()
    self._current_id: Optional[str] = None  # None: not started, or closed
    self._owned = False
    self._generation = 0
    self._handles: dict[asyncio.AbstractEventLoop, tuple[int, AsyncSandbox]] = (
        {}
    )
    self._last: Optional[AsyncSandbox] = None
    self._sandbox_id: Optional[str] = sandbox_id
    # Set by close() with "pause" or "keep": the next initialize() picks the
    # same sandbox up again instead of making a new one.
    self._resume: Optional[tuple[str, bool]] = None
    self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
    self._path: Optional[tuple[str, str]] = None  # (sandbox id, PATH)
    self._replaced = False
    # Older ADK releases have no `is_initialized` on BaseEnvironment.
    self._is_initialized = False

  @property
  @override
  def working_dir(self) -> Path:
    if self._current_id is None:
      raise RuntimeError(_NOT_STARTED)
    return Path(self._working_dir)

  @property
  def sandbox_id(self) -> Optional[str]:
    """The sandbox's id (``sbx_...``), also after ``close()``.

    Pass it to ``PrizedEnvironment(sandbox_id=...)`` to continue in the same
    sandbox later (with ``on_close="pause"`` or ``"keep"``).
    """
    return self._sandbox_id

  @property
  def sandbox(self) -> AsyncSandbox:
    """The ``prized.AsyncSandbox`` behind this environment, for anything else
    the Prized SDK offers (background commands, ``pause()``, ``fork()``).

    Use it from the event loop that last ran an operation on the environment.
    """
    try:
      loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
      loop = None
    with self._state:
      if self._current_id is None:
        raise RuntimeError(_NOT_STARTED)
      cached = self._handles.get(loop) if loop is not None else None
      if cached is not None and cached[0] == self._generation:
        return cached[1]
      if self._last is not None and self._last.id == self._current_id:
        return self._last
    raise RuntimeError("No handle yet: run an operation on this loop first.")

  @override
  async def initialize(self) -> None:
    if self._current_id is not None:
      return
    loop = asyncio.get_running_loop()
    async with self._lock_for(loop):
      with self._state:
        if self._current_id is not None:
          return
        generation, resume = self._generation, self._resume
      if resume is not None:
        sandbox, created, replaced = await self._attach(*resume)
        owned = resume[1]
      elif self._requested_sandbox_id is not None:
        sandbox, created, replaced = await self._attach(
            self._requested_sandbox_id, False
        )
        owned = False
      else:
        sandbox, created, replaced = await self._create_sandbox(), True, False
        owned = True
      if self._publish(loop, generation, sandbox, owned, replaced=replaced):
        with self._state:
          if self._resume == resume:
            self._resume = None
      elif created:  # another thread got there first: use its sandbox
        await _discard(sandbox)
      else:
        await _close_quietly(sandbox)
      self._is_initialized = True

  @override
  async def close(self) -> None:
    from prized import PrizedError

    with self._state:
      sandbox_id, owned = self._current_id, self._owned
    if sandbox_id is None:
      return
    action = self._on_close or ("delete" if owned else "keep")
    try:
      sandbox = await self._acquire(replace=False)
    except RuntimeError:
      if self._current_id is None:
        return  # closed by another thread meanwhile
      raise
    if sandbox is not None:
      sandbox_id = sandbox.id
      try:
        if action == "delete":
          await sandbox.delete()
        elif action == "pause":
          # Pausing settles on its own; ADK gives a toolset's close 10 s.
          await sandbox.pause(wait=False)
      except PrizedError as e:
        if e.code != "not_found":
          raise  # nothing is released, so the close can be retried
    with self._state:
      self._current_id = None
      self._generation += 1  # a call still in flight can't replace it now
      self._handles = {}
      # Kept or paused: the next initialize() picks it up (or, if it is gone
      # by then, a sandbox this environment created is replaced).
      if action != "delete":
        self._resume = (sandbox_id, owned)
    self._is_initialized = False
    if sandbox is not None:
      await _close_quietly(sandbox)

  @override
  async def execute(
      self,
      command: str,
      *,
      timeout: Optional[float] = None,
  ) -> ExecutionResult:
    from prized import CommandTimeoutError
    from prized import PrizedError

    sandbox = await self._ensure_sandbox()
    try:
      try:
        result = await self._exec(sandbox, command, timeout)
      except PrizedError as e:
        if isinstance(e, CommandTimeoutError):
          raise
        sandbox = await self._replace_if_gone(sandbox, e)
        result = await self._exec(sandbox, command, timeout)
    except CommandTimeoutError as e:
      return self._note(_timed_out(e))
    return self._note(
        ExecutionResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    )

  @override
  async def read_file(self, path: str | os.PathLike[str]) -> bytes:
    from prized import PrizedError

    resolved = self._resolve_path(path)
    sandbox = await self._ensure_sandbox()
    if _inside_home(resolved):
      try:
        return await sandbox.files.read_bytes(resolved)
      except PrizedError as e:
        missing = FileNotFoundError(
            errno.ENOENT, os.strerror(errno.ENOENT), resolved
        )
        if e.code == "not_found":
          # A missing file, or a missing sandbox: replace a gone sandbox (a
          # fresh one has no such file either), then report the file.
          try:
            await self._replace_if_gone(sandbox, e, missing_file=True)
          except PrizedError as check:
            raise missing from check
          raise missing from e
        if e.code == "not_regular_file":
          # A FileNotFoundError, which ADK's file tools report to the model
          # (anything else ends the run).
          raise FileNotFoundError(
              errno.EISDIR, "Not a regular file", resolved
          ) from e
        if e.code == "file_too_large":
          raise OSError(errno.EFBIG, e.message, resolved) from e
        if e.code not in _SHELL_FALLBACK_CODES:
          raise
    # Outside /home/prized, or a symlink the file API refuses: the shell.
    result = await self._helper(["base64", "-w0", "--", resolved])
    if result.exit_code != 0:
      error = _os_error(result.stderr, resolved)
      if isinstance(error, (IsADirectoryError, NotADirectoryError)):
        raise FileNotFoundError(error.errno, error.strerror, resolved)
      raise error
    if result.truncated:
      raise OSError(
          errno.EFBIG,
          "File too large to read through the shell (about 3 MiB at most)",
          resolved,
      )
    return base64.b64decode(result.stdout)

  @override
  async def write_file(
      self, path: str | os.PathLike[str], content: str | bytes
  ) -> None:
    from prized import PrizedError

    resolved = self._resolve_path(path)
    data = content.encode("utf-8") if isinstance(content, str) else content
    staging = f"{_STAGING_DIR}/{uuid.uuid4().hex}"
    sandbox = await self._ensure_sandbox()
    try:
      await sandbox.files.write(staging, data)
    except PrizedError as e:
      sandbox = await self._replace_if_gone(sandbox, e)
      await sandbox.files.write(staging, data)
    # `cat >` writes into an existing file, so it keeps its mode (a script
    # stays executable), its owner and its links.
    script = (
        "trap 'rm -f -- \"$1\"' EXIT; "
        'mkdir -p -- "$(dirname -- "$2")" && cat -- "$1" > "$2"'
    )
    result = await self._helper(
        ["bash", "-c", script, "write_file", staging, resolved]
    )
    if result.exit_code != 0:
      raise _os_error(result.stderr, resolved)

  async def pause(self) -> None:
    """Pause the sandbox now: memory, disk and processes are kept, and no
    compute is charged. The next operation resumes it."""
    sandbox = await self._ensure_sandbox()
    await sandbox.pause()

  async def fork(
      self, count: int = 1, *, on_close: OnClose = "delete"
  ) -> list[PrizedEnvironment]:
    """Fork the sandbox into ``count`` live copies.

    Each copy starts with this sandbox's files, memory and running processes
    and goes its own way from then on. The returned environments are
    initialized; closing one deletes its copy unless ``on_close`` says
    otherwise.
    """
    sandbox = await self._ensure_sandbox()
    copies = await sandbox.fork(count)
    loop = asyncio.get_running_loop()
    environments = []
    for copy in copies:
      env = PrizedEnvironment(
          sandbox_id=copy.id,
          working_dir=self._working_dir,
          on_close=on_close,
          api_key=self._api_key,
          base_url=self._base_url,
      )
      # A copy that is ever replaced gets the parent's shape, env and labels.
      env._template = self._template
      env._create_kwargs = dict(self._create_kwargs)
      env._publish(loop, 0, copy, owned=True)
      env._is_initialized = True
      environments.append(env)
    return environments

  async def _exec(
      self, sandbox: AsyncSandbox, command: str, timeout: Optional[float]
  ) -> ExecResult:
    env = {**_PIP_ENV, "PATH": await self._command_path(sandbox)}
    # None means no limit here; the SDK spells that 0.
    return await sandbox.exec(
        command,
        cwd=self._working_dir,
        env=env,
        timeout=0 if timeout is None else timeout,
    )

  async def _helper(self, argv: list[str]) -> ExecResult:
    """A short helper command, retried once on a replacement if it was gone."""
    from prized import PrizedError

    sandbox = await self._ensure_sandbox()
    try:
      return await sandbox.exec(argv, timeout=_HELPER_TIMEOUT)
    except PrizedError as e:
      sandbox = await self._replace_if_gone(sandbox, e)
      return await sandbox.exec(argv, timeout=_HELPER_TIMEOUT)

  async def _command_path(self, sandbox: AsyncSandbox) -> str:
    """The sandbox's PATH with ~/.local/bin first, so pip-installed tools run."""
    cached = self._path
    if cached is None or cached[0] != sandbox.id:
      found = await sandbox.exec(["printenv", "PATH"], timeout=_HELPER_TIMEOUT)
      parts = (found.stdout.strip() or _FALLBACK_PATH).split(":")
      path = ":".join([_LOCAL_BIN] + [p for p in parts if p != _LOCAL_BIN])
      cached = self._path = (sandbox.id, path)
    return cached[1]

  async def _create_sandbox(self) -> AsyncSandbox:
    try:
      from prized import AsyncSandbox
    except ImportError as e:
      raise ImportError(
          "The prized package is required to use PrizedEnvironment. Install"
          " it with `pip install prized-adk`."
      ) from e

    sandbox = await AsyncSandbox.create(
        self._template,
        api_key=self._api_key,
        base_url=self._base_url,
        **self._create_kwargs,
    )
    if self._working_dir != _DEFAULT_WORKING_DIR:
      try:
        await self._make_working_dir(sandbox)
      except BaseException:
        await _discard(sandbox)
        raise
    return sandbox

  async def _connect(self, sandbox_id: str) -> Optional[AsyncSandbox]:
    """A handle on this event loop, or None once the sandbox no longer exists."""
    from prized import AsyncSandbox
    from prized import PrizedError

    try:
      return await AsyncSandbox.connect(
          sandbox_id, api_key=self._api_key, base_url=self._base_url
      )
    except PrizedError as e:
      if e.code == "not_found":
        return None
      raise

  async def _attach(
      self, sandbox_id: str, owned: bool
  ) -> tuple[AsyncSandbox, bool, bool]:
    """A handle on ``sandbox_id``: (handle, created, replaced).

    A gone sandbox this environment created is replaced by a fresh one.
    """
    sandbox = await self._connect(sandbox_id)
    if sandbox is None or sandbox.state in _GONE_STATES:
      state = "gone" if sandbox is None else sandbox.state
      if sandbox is not None:
        if owned and sandbox.state == "error":
          await _discard(sandbox)  # it still counts toward quota
        else:
          await _close_quietly(sandbox)
      if not owned:
        raise RuntimeError(f"Sandbox {sandbox_id} is {state}.")
      logger.warning(
          "Prized sandbox %s is %s; creating a fresh one. Workspace state"
          " (installed packages and files) has been lost.",
          sandbox_id,
          state,
      )
      return await self._create_sandbox(), True, True
    if self._working_dir != _DEFAULT_WORKING_DIR:
      await self._make_working_dir(sandbox)
    return sandbox, False, False

  async def _make_working_dir(self, sandbox: AsyncSandbox) -> None:
    result = await sandbox.exec(
        ["mkdir", "-p", "--", self._working_dir], timeout=_HELPER_TIMEOUT
    )
    if result.exit_code != 0:
      raise _os_error(result.stderr, self._working_dir)

  def _publish(
      self,
      loop: asyncio.AbstractEventLoop,
      generation: int,
      sandbox: AsyncSandbox,
      owned: bool,
      *,
      replaced: bool = False,
  ) -> bool:
    """Makes ``sandbox`` the one in use, unless another thread moved on since
    ``generation`` was read; then the caller still owns ``sandbox``."""
    with self._state:
      if self._generation != generation:
        return False
      self._generation += 1
      self._current_id = self._sandbox_id = sandbox.id
      self._owned = owned
      self._handles = {loop: (self._generation, sandbox)}
      self._last = sandbox
      if replaced:
        self._replaced = True
      return True

  def _lock_for(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
    """Serializes the tasks of one event loop (threads use `_publish`)."""
    with self._state:
      for stale in [lp for lp in self._locks if lp.is_closed()]:
        del self._locks[stale]
        self._handles.pop(stale, None)
      lock = self._locks.get(loop)
      if lock is None:
        lock = self._locks[loop] = asyncio.Lock()
      return lock

  async def _ensure_sandbox(self) -> AsyncSandbox:
    loop = asyncio.get_running_loop()
    with self._state:
      if self._current_id is None:
        raise RuntimeError(_NOT_STARTED)
      cached = self._handles.get(loop)
      if cached is not None and cached[0] == self._generation:
        return cached[1]
    sandbox = await self._acquire()
    if sandbox is None:  # pragma: no cover - only without replace
      raise RuntimeError(_NOT_STARTED)
    return sandbox

  async def _acquire(
      self,
      *,
      failed: Optional[AsyncSandbox] = None,
      replace: bool = True,
  ) -> Optional[AsyncSandbox]:
    """This loop's handle on the sandbox in use, connecting or replacing it.

    With ``failed`` (a handle an operation just failed on), a handle that is
    still current is checked: if its sandbox is fine it is returned as it is.
    With ``replace=False`` (closing), a gone sandbox gives None.
    """
    from prized import PrizedError

    loop = asyncio.get_running_loop()
    async with self._lock_for(loop):
      for _ in range(5):
        with self._state:
          generation, sandbox_id = self._generation, self._current_id
          owned = self._owned
          cached = self._handles.get(loop)
        if sandbox_id is None:
          raise RuntimeError(_NOT_STARTED)  # closed meanwhile: no new one
        current = (
            cached[1]
            if cached is not None and cached[0] == generation
            else None
        )
        if current is not None and current is not failed:
          return current
        if current is not None:  # the failed handle is still current
          handle: Optional[AsyncSandbox] = current
          try:
            await current.refresh()
            gone = current.state in _GONE_STATES
          except PrizedError as e:
            if e.code != "not_found":
              raise
            gone = True
          if not gone:
            return current
        else:
          handle = await self._connect(sandbox_id)
          gone = handle is None or handle.state in _GONE_STATES
          if handle is not None and not gone:
            with self._state:
              if self._generation == generation:
                self._handles[loop] = (generation, handle)
                self._last = handle
                return handle
            await _close_quietly(handle)  # moved on meanwhile: look again
            continue
        # The sandbox is gone.
        if handle is not None and owned and handle.state == "error":
          await _discard(handle)  # it still counts toward quota
          handle = None
        if not replace:
          if handle is not None and handle is not current:
            await _close_quietly(handle)
          return None
        if not owned:
          raise RuntimeError(f"Sandbox {sandbox_id} is gone.")
        new = await self._create_sandbox()
        if self._publish(loop, generation, new, owned=True, replaced=True):
          logger.warning(
              "Prized sandbox %s is gone; created %s. Workspace state"
              " (installed packages and files) has been lost.",
              sandbox_id,
              new.id,
          )
          return new
        await _discard(new)  # another thread replaced it first: use theirs
      raise RuntimeError("Could not settle on a sandbox; try again.")

  async def _replace_if_gone(
      self,
      failed: AsyncSandbox,
      error: PrizedError,
      *,
      missing_file: bool = False,
  ) -> AsyncSandbox:
    """Returns a sandbox to retry on after ``error`` on ``failed``, or raises.

    Only a sandbox this environment created is replaced, and only once it is
    gone; anything else re-raises ``error`` (or, with ``missing_file``,
    returns ``failed`` so the caller reports the file).
    """
    if error.code not in _MAYBE_GONE_CODES:
      raise error
    sandbox = await self._acquire(failed=failed)
    if sandbox is failed:
      if missing_file:
        return failed
      raise error
    assert sandbox is not None
    return sandbox

  def _note(self, result: ExecutionResult) -> ExecutionResult:
    """Tells the model, once, that its sandbox was replaced."""
    with self._state:
      replaced, self._replaced = self._replaced, False
    if replaced:
      result.stderr = _REPLACED_NOTE + result.stderr
    return result

  def _resolve_path(self, path: str | os.PathLike[str]) -> str:
    """Resolve a path against the working directory and normalize it."""
    raw = os.fspath(path)
    if not raw:
      raise ValueError("path must not be empty")
    return posixpath.normpath(posixpath.join(self._working_dir, raw))


def _inside_home(path: str) -> bool:
  return path == _HOME or path.startswith(_HOME + "/")


async def _close_quietly(sandbox: AsyncSandbox) -> None:
  with contextlib.suppress(Exception):
    await sandbox.close()


async def _discard(sandbox: AsyncSandbox) -> None:
  """Deletes a sandbox this environment made and no longer uses."""
  with contextlib.suppress(Exception):
    await sandbox.delete()
  await _close_quietly(sandbox)


def _timed_out(error: Exception) -> ExecutionResult:
  result = getattr(error, "result", None)
  return ExecutionResult(
      exit_code=_TIMEOUT_EXIT_CODE,
      stdout=getattr(result, "stdout", "") or "",
      stderr=getattr(result, "stderr", "") or "",
      timed_out=True,
  )


def _os_error(stderr: str, path: str) -> OSError:
  """The OSError a failed shell helper stands for."""
  message = stderr.strip() or "command failed"
  for needle, number, kind in (
      ("No such file or directory", errno.ENOENT, FileNotFoundError),
      ("Is a directory", errno.EISDIR, IsADirectoryError),
      ("Permission denied", errno.EACCES, PermissionError),
      ("Not a directory", errno.ENOTDIR, NotADirectoryError),
  ):
    if needle in message:
      return kind(number, os.strerror(number), path)
  return OSError(errno.EIO, message, path)
