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

"""PrizedCodeExecutionToolkit: CAMEL's CodeExecutionToolkit with PrizedInterpreter as its sandbox."""

from __future__ import annotations

from types import TracebackType
from typing import Any

from camel.toolkits import CodeExecutionToolkit

from .interpreter import PrizedInterpreter


class PrizedCodeExecutionToolkit(CodeExecutionToolkit):
    r"""CAMEL's ``CodeExecutionToolkit`` (the ``execute_code`` and ``execute_command`` tools), running
    in a Prized sandbox. ``CodeExecutionToolkit(sandbox=...)`` takes only CAMEL's built-in sandbox
    names, so this subclass puts a :class:`PrizedInterpreter` in place of one.

    Args:
        verbose (bool): Print each execution's result. (default: :obj:`False`)
        timeout (Optional[float]): Seconds each run or command may take before it is stopped.
            (default: :obj:`None`: 120 seconds)
        interpreter (Optional[PrizedInterpreter]): Use this interpreter (and its sandbox).
        **interpreter_options: Otherwise, options for a new :class:`PrizedInterpreter`
            (``template``, ``vcpu``, ``memory_gb``, ``sandbox_id``, ``api_key``, ...).
    """

    def __init__(
        self,
        verbose: bool = False,
        timeout: float | None = None,
        interpreter: PrizedInterpreter | None = None,
        **interpreter_options: Any,
    ) -> None:
        if interpreter is not None and (interpreter_options or timeout is not None):
            raise ValueError("Pass either interpreter= or options (timeout too) for a new one, not both.")
        if interpreter is None:
            if timeout is not None:
                interpreter_options.setdefault("timeout", timeout)
            interpreter = PrizedInterpreter(**interpreter_options)
        # The parent sets up the tools and MCP server; its throwaway local interpreter is replaced
        # before any code runs. Its own timeout (a thread per call) stays off: the sandbox enforces
        # the interpreter's timeout and stops the command, which a thread timeout would leave running.
        super().__init__(sandbox="subprocess", verbose=verbose, require_confirm=False, timeout=None)
        self.interpreter = interpreter  # type: ignore[assignment]
        self.prized_interpreter = interpreter

    @property
    def sandbox_id(self) -> str | None:
        """The sandbox's id, once it exists."""
        return self.prized_interpreter.sandbox_id

    def close(self) -> None:
        """Delete the sandbox if the interpreter created it."""
        self.prized_interpreter.close()

    def __enter__(self) -> PrizedCodeExecutionToolkit:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
