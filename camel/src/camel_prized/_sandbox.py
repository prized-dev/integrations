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

"""One Prized sandbox, made on first use and shared by everything that runs code in it."""

from __future__ import annotations

import logging
import os
import shlex
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar

import prized
from prized import CommandTimeoutError, ExecResult, PrizedError

logger = logging.getLogger("camel_prized")

T = TypeVar("T")

DEFAULT_TEMPLATE = "prized-base-v1"
DEFAULT_TIMEOUT = 120.0
DEFAULT_AUTO_PAUSE_MIN = 10
DEFAULT_AUTO_DELETE_MIN = 60
LABELS = {"created-by": "camel"}
WORKDIR = "/home/prized/work"
# Every command starts with this, in new and attached sandboxes alike. The templates' Python is
# "externally managed": without PIP_BREAK_SYSTEM_PACKAGES, `pip install` fails. With it, pip installs
# for the user into ~/.local, and ~/.local/bin on the PATH makes the commands it installs work.
COMMAND_PREFIX = 'export PATH="$HOME/.local/bin:$PATH" PIP_BREAK_SYSTEM_PACKAGES=1; '
# Code and commands go inline (one argument) while the command stays under this; longer ones are
# uploaded as a file first (Linux caps a single argument at 128 KiB).
MAX_INLINE_COMMAND = 64 * 1024
# A command with a timeout over the SDK's blocking limit runs detached, and the SDK quotes it again
# into a start script, which can grow it several times over: such commands go inline only when short.
MAX_BLOCKING_TIMEOUT = 240.0
MAX_INLINE_DETACHED = 8 * 1024
API_KEYS_URL = "https://prized.dev/dashboard/workspace/api-keys?via=camel"
# Errors that may mean the sandbox is gone (its run time ended, or it was deleted after pausing).
_MAYBE_GONE_CODES = frozenset({"not_found", "sandbox_not_running", "sandbox_deleted"})
_GONE_STATES = frozenset({"deleted", "deleting", "error"})
GONE_NOTE = "(The previous sandbox was gone, so this ran in a new, empty one: files from earlier runs are not here.)\n"


def resolve_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("PRIZED_API_KEY")
    if not key:
        raise ValueError(
            "Prized needs an API key: pass api_key= or set PRIZED_API_KEY. "
            f"Make one with Sandboxes: Write access at {API_KEYS_URL}"
        )
    return key


class SandboxHolder:
    """Creates the sandbox on first use (or attaches to ``sandbox_id``), replaces one it created
    if it is found gone, and deletes it on :meth:`close` when it created it."""

    def __init__(
        self,
        *,
        sandbox_id: str | None = None,
        template: str = DEFAULT_TEMPLATE,
        vcpu: int | None = None,
        memory_gb: float | None = None,
        disk_gb: int | None = None,
        env_vars: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
        auto_pause_min: int | None = DEFAULT_AUTO_PAUSE_MIN,
        auto_delete_min: int | None = DEFAULT_AUTO_DELETE_MIN,
        max_runtime_min: int | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._api_key = resolve_api_key(api_key)
        self._base_url = base_url
        self._attach_id = sandbox_id
        self._create_kwargs: dict[str, object] = {
            "vcpu": vcpu,
            "memory_gb": memory_gb,
            "disk_gb": disk_gb,
            "env_vars": dict(env_vars) if env_vars else None,
            "labels": {**LABELS, **dict(labels or {})},
            "auto_pause_min": auto_pause_min,
            "auto_delete_min": auto_delete_min,
            "max_runtime_min": max_runtime_min,
        }
        self._template = template
        self._lock = threading.RLock()
        self._sandbox: prized.Sandbox | None = None
        self._closed = False

    @property
    def owned(self) -> bool:
        """True when this object creates the sandbox (and so deletes it on close)."""
        return self._attach_id is None

    @property
    def sandbox_id(self) -> str | None:
        """The sandbox's id, once there is one."""
        sandbox = self._sandbox
        return sandbox.id if sandbox is not None else self._attach_id

    @property
    def sandbox(self) -> prized.Sandbox | None:
        """The ``prized.Sandbox`` in use, or None before the first run."""
        return self._sandbox

    def get(self) -> prized.Sandbox:
        with self._lock:
            if self._closed:
                raise RuntimeError("This Prized sandbox has been closed; make a new one.")
            if self._sandbox is None:
                if self._attach_id is not None:
                    self._sandbox = prized.Sandbox.connect(
                        self._attach_id, api_key=self._api_key, base_url=self._base_url
                    )
                else:
                    self._sandbox = prized.Sandbox.create(
                        self._template,
                        api_key=self._api_key,
                        base_url=self._base_url,
                        **self._create_kwargs,  # type: ignore[arg-type]
                    )
                    logger.info("Created Prized sandbox %s", self._sandbox.id)
            return self._sandbox

    def attempt(self, step: Callable[[prized.Sandbox], T]) -> tuple[T, bool]:
        """Run ``step`` on the sandbox. If the sandbox this object created turns out to be gone,
        run it once more on a new one. Returns the step's result and whether it was replaced."""
        sandbox = self.get()
        try:
            return step(sandbox), False
        except CommandTimeoutError:
            raise
        except PrizedError as err:
            if not self._replace_if_gone(sandbox, err):
                raise
        return step(self.get()), True

    def _replace_if_gone(self, sandbox: prized.Sandbox, err: PrizedError) -> bool:
        if not self.owned or err.code not in _MAYBE_GONE_CODES:
            return False
        state: str | None = None
        try:
            state = sandbox.refresh().state
            gone = state in _GONE_STATES
        except PrizedError as again:
            gone = again.code == "not_found"
        if not gone:
            return False
        with self._lock:
            if self._sandbox is sandbox:
                logger.warning("Prized sandbox %s is %s; the next run gets a new one.", sandbox.id, state or "gone")
                if state == "error":
                    try:  # a failed sandbox still counts toward the workspace's quota
                        sandbox.delete()
                    except PrizedError as err_delete:
                        logger.warning("Could not delete failed sandbox %s: %s", sandbox.id, err_delete)
                sandbox.close()
                self._sandbox = None
        return True

    def run_program(
        self, inline: Sequence[str], by_file: Sequence[str], suffix: str, code: str, timeout: float
    ) -> tuple[ExecResult, bool, bool]:
        """Run ``code`` with ``inline + [code]`` (``python3 -c CODE``), or, when that command would be
        too long for one argument, upload it into the working directory and run ``by_file + [path]``.
        Returns the result, whether it timed out, and whether the sandbox was replaced."""
        return self._run(COMMAND_PREFIX + shlex.join([*inline, code]), by_file, suffix, code, timeout)

    def run_shell(self, command: str, timeout: float) -> tuple[ExecResult, bool, bool]:
        """Run one shell command (uploaded as a script first when it is long). Returns the result,
        whether it timed out, and whether the sandbox was replaced."""
        return self._run(COMMAND_PREFIX + command, ("bash",), ".sh", command, timeout)

    def _run(
        self, command: str, by_file: Sequence[str], suffix: str, payload: str, timeout: float
    ) -> tuple[ExecResult, bool, bool]:
        limit = MAX_INLINE_COMMAND if timeout <= MAX_BLOCKING_TIMEOUT else MAX_INLINE_DETACHED
        if len(command.encode()) <= limit:
            (result, timed_out), replaced = self.attempt(lambda sandbox: _exec(sandbox, command, timeout))
            return result, timed_out, replaced

        def by_upload(sandbox: prized.Sandbox) -> tuple[ExecResult, bool]:
            # In the working directory, so imports and relative paths resolve as they do inline.
            path = f"{WORKDIR}/.camel-run-{uuid.uuid4().hex}{suffix}"
            sandbox.files.write(path, payload)
            quoted = shlex.quote(path)
            script = f"{COMMAND_PREFIX}{shlex.join(by_file)} {quoted}; rc=$?; rm -f {quoted}; exit $rc"
            return _exec(sandbox, script, timeout)

        (result, timed_out), replaced = self.attempt(by_upload)
        return result, timed_out, replaced

    def close(self) -> None:
        """Delete the sandbox if this object created it (else just let go of it). Safe to call twice.
        If the delete fails, the error is raised and the sandbox is kept, so calling again retries."""
        with self._lock:
            self._closed = True
            sandbox = self._sandbox
            if sandbox is None:
                return
            if self.owned:
                sandbox.delete()
                logger.info("Deleted Prized sandbox %s", sandbox.id)
            self._sandbox = None
        sandbox.close()

    def reopen(self) -> None:
        """Allow use again after :meth:`close` (the next run creates a fresh sandbox)."""
        with self._lock:
            self._closed = False


def _exec(sandbox: prized.Sandbox, command: str, timeout: float) -> tuple[ExecResult, bool]:
    try:
        return sandbox.exec(command, timeout=timeout), False
    except CommandTimeoutError as err:
        result = err.result if err.result is not None else ExecResult("", "", -1, timed_out=True)
        return result, True


def format_output(result: ExecResult, timed_out: bool, timeout: float, replaced: bool) -> str:
    """The text a model sees, in the shape of CAMEL's SubprocessInterpreter: stdout, then
    ``(stderr: ...)``, then a note on a failure, a timeout or a cut-off."""
    text = GONE_NOTE if replaced else ""
    text += result.stdout
    if result.stderr:
        text += f"(stderr: {result.stderr})"
    if result.truncated:
        text += "(The output was cut off at 4 MiB per stream: write large output to a file instead.)"
    if timed_out:
        text += f"(Execution timed out after {timeout:g} seconds and was stopped.)"
    elif result.exit_code != 0:
        text += f"(Execution failed with return code {result.exit_code})"
    return text or "Code executed successfully (no output)."
