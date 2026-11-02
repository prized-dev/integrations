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

"""PrizedInterpreter: CAMEL's BaseInterpreter on a Prized sandbox."""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Any, ClassVar

import prized
from camel.interpreters import BaseInterpreter, InterpreterError
from prized import PrizedError

from ._sandbox import (
    DEFAULT_AUTO_DELETE_MIN,
    DEFAULT_AUTO_PAUSE_MIN,
    DEFAULT_TEMPLATE,
    DEFAULT_TIMEOUT,
    SandboxHolder,
    format_output,
    logger,
)

# language -> (inline argv before the code, the command for an uploaded file, file suffix). Python
# and Node read an uploaded file from stdin, so imports resolve from the working directory as inline.
_LANGUAGES: dict[str, tuple[tuple[str, ...], str, str]] = {
    "python": (("python3", "-c"), "python3 - < {path}", ".py"),
    "javascript": (("node", "-e"), "node - < {path}", ".js"),
    "bash": (("bash", "-c"), "bash {path}", ".sh"),
}


class PrizedInterpreter(BaseInterpreter):
    r"""Runs code and shell commands in a Prized sandbox: a Firecracker microVM with its own kernel
    (Ubuntu 24.04 with Python 3, Node.js 22 and git in the default template).

    The sandbox is created on the first run, or by :meth:`start`, and is kept for every later run, so
    files and installed packages carry over. Each run is a new process: variables do not. Close the
    interpreter (``close()``, or a ``with`` block) to delete the sandbox; one left behind pauses
    itself after ``auto_pause_min`` idle minutes and is deleted ``auto_delete_min`` minutes later.

    Args:
        require_confirm (bool): Ask on the terminal before each run. (default: :obj:`False`: the
            code runs in a remote microVM, not on this machine)
        sandbox_id (Optional[str]): Use this existing sandbox instead of creating one. It is not
            deleted on close, and the options for a new sandbox below do not apply.
        template (str): ``prized-base-v1``, or ``prized-agent-v1`` (adds Claude Code and Codex).
        vcpu (Optional[int]): vCPUs for a new sandbox (template default when None).
        memory_gb (Optional[float]): Memory for a new sandbox, in GB.
        disk_gb (Optional[int]): Disk for a new sandbox, in GB.
        env_vars (Optional[Mapping[str, str]]): Environment for a new sandbox. Code in the sandbox,
            the model's included, can read it: put there only what that code needs.
        labels (Optional[Mapping[str, str]]): Labels for a new sandbox, to find it later.
        auto_pause_min (Optional[int]): Idle minutes before a new sandbox pauses itself. (default: 10)
        auto_delete_min (Optional[int]): Minutes after pausing before a new sandbox is deleted.
            (default: 60)
        max_runtime_min (Optional[int]): Running minutes before a new sandbox is stopped (plan
            default when None).
        timeout (float): Seconds a run or command may take before it is stopped. (default: 120)
        api_key (Optional[str]): A Prized API key with Sandboxes: Write. Defaults to the
            ``PRIZED_API_KEY`` environment variable.
        base_url (Optional[str]): The Prized API origin. (default: https://api.prized.dev)

    Environment Variables:
        PRIZED_API_KEY: The API key, when ``api_key`` is not given.
    """

    _CODE_TYPE_MAPPING: ClassVar[dict[str, str]] = {
        "python": "python",
        "py3": "python",
        "python3": "python",
        "py": "python",
        "javascript": "javascript",
        "js": "javascript",
        "node": "javascript",
        "bash": "bash",
        "shell": "bash",
        "sh": "bash",
    }

    def __init__(
        self,
        require_confirm: bool = False,
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
        timeout: float = DEFAULT_TIMEOUT,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        self.require_confirm = require_confirm
        self.timeout = timeout
        self._holder = SandboxHolder(
            sandbox_id=sandbox_id,
            template=template,
            vcpu=vcpu,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            env_vars=env_vars,
            labels=labels,
            auto_pause_min=auto_pause_min,
            auto_delete_min=auto_delete_min,
            max_runtime_min=max_runtime_min,
            api_key=api_key,
            base_url=base_url,
        )

    # --- lifecycle --------------------------------------------------------------------------

    @property
    def sandbox_id(self) -> str | None:
        """The sandbox's id, once it exists (or the one given to attach to)."""
        return self._holder.sandbox_id

    @property
    def sandbox(self) -> prized.Sandbox | None:
        """The ``prized.Sandbox`` in use (None before the first run), for anything else in the SDK:
        ``files``, ``pause()``, ``fork()``."""
        return self._holder.sandbox

    def start(self) -> PrizedInterpreter:
        """Create (or attach to) the sandbox now rather than on the first run."""
        self._wrap(self._holder.get)
        return self

    def close(self) -> None:
        """Delete the sandbox if this interpreter created it. The interpreter can't be used after."""
        self._holder.close()

    def __enter__(self) -> PrizedInterpreter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        # Best effort, like CAMEL's E2BInterpreter: a sandbox missed here still pauses itself when
        # idle and is deleted auto_delete_min minutes later.
        holder = getattr(self, "_holder", None)
        if holder is None:
            return
        try:
            holder.close()
        except Exception as err:
            logger.warning("Could not delete Prized sandbox %s: %s", holder.sandbox_id, err)

    # --- BaseInterpreter --------------------------------------------------------------------

    def run(self, code: str, code_type: str = "python") -> str:
        r"""Runs ``code`` in the sandbox and returns what it printed.

        Args:
            code (str): The code to run.
            code_type (str): One of :meth:`supported_code_types` (Python, JavaScript on Node.js, or
                bash). (default: :obj:`"python"`)

        Returns:
            str: stdout, then ``(stderr: ...)`` and a note when the code failed or timed out.

        Raises:
            InterpreterError: If ``code_type`` is not supported, the user declined to run the code,
                or the sandbox could not be reached.
        """
        language = self._CODE_TYPE_MAPPING.get(code_type.lower()) if isinstance(code_type, str) else None
        if language is None:
            raise InterpreterError(
                f"Unsupported code type {code_type}. `{self.__class__.__name__}` only supports "
                f"{', '.join(self._CODE_TYPE_MAPPING)}."
            )
        self._confirm(f"The following {code_type} code will run in a Prized sandbox:\n{code}")
        inline, by_file, suffix = _LANGUAGES[language]
        result, timed_out, replaced = self._wrap(
            lambda: self._holder.run_program(inline, by_file, suffix, code, self.timeout)
        )
        return format_output(result, timed_out, self.timeout, replaced)

    def execute_command(self, command: str) -> str:
        r"""Runs a shell command in the sandbox, e.g. ``pip install numpy`` (no root needed).

        Args:
            command (str): The command, run by bash in /home/prized/work.

        Returns:
            str: stdout, then ``(stderr: ...)`` and a note when it failed or timed out.
        """
        self._confirm(f"The following shell command will run in a Prized sandbox:\n{command}")
        result, timed_out, replaced = self._wrap(lambda: self._holder.run_shell(command, self.timeout))
        return format_output(result, timed_out, self.timeout, replaced)

    def supported_code_types(self) -> list[str]:
        r"""Provides supported code types by the interpreter."""
        return list(self._CODE_TYPE_MAPPING)

    def update_action_space(self, action_space: dict[str, Any]) -> None:
        r"""Not supported: the code runs in another machine, where these Python objects don't exist."""
        raise RuntimeError("PrizedInterpreter doesn't support `action_space`.")

    # --- helpers ----------------------------------------------------------------------------

    def _confirm(self, prompt: str) -> None:
        if not self.require_confirm:
            return
        logger.info(prompt)
        while True:
            choice = input("Running code? [Y/n]:").lower().strip()
            if choice in ("y", "yes", "ye", ""):
                return
            if choice in ("n", "no"):
                raise InterpreterError(
                    "Execution halted: User opted not to run the code. This choice stops the current "
                    "operation and any further code execution."
                )

    def _wrap(self, call: Any) -> Any:
        try:
            return call()
        except PrizedError as err:
            raise InterpreterError(f"Prized sandbox error ({err.code}): {err}") from err
