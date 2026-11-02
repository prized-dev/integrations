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

"""PrizedRuntime: CAMEL's BaseRuntime on a Prized sandbox, in the shape of DaytonaRuntime."""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import uuid
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any

import prized
from camel.runtimes import BaseRuntime
from camel.toolkits import FunctionTool
from prized import PrizedError
from pydantic_core import to_jsonable_python

from ._sandbox import (
    DEFAULT_AUTO_DELETE_MIN,
    DEFAULT_AUTO_PAUSE_MIN,
    DEFAULT_TEMPLATE,
    DEFAULT_TIMEOUT,
    SandboxHolder,
    logger,
)

# Runs one function in the sandbox: its source, then the call, then the JSON result after a marker
# line, so whatever the function prints itself doesn't get mixed into the result.
_RUNNER = """\
import json, sys, traceback
_marker = {marker!r}
_ns = {{"__name__": "__camel_prized__"}}
try:
    exec(compile({source!r}, {filename!r}, "exec"), _ns)
    _result = {{"ok": _ns[{name!r}](*json.loads({args!r}), **json.loads({kwargs!r}))}}
except BaseException as _err:
    _result = {{"error": f"{{type(_err).__name__}}: {{_err}}", "traceback": traceback.format_exc()}}
sys.stdout.flush()
try:
    _out = json.dumps(_result, default=str)
except (TypeError, ValueError) as _err:
    _out = json.dumps({{"error": f"The result could not be sent back as JSON: {{_err}}"}})
sys.stdout.write("\\n" + _marker + _out + "\\n")
"""


class PrizedRuntime(BaseRuntime):
    r"""Runs CAMEL tools (``FunctionTool``) in a Prized sandbox: a Firecracker microVM with its own
    kernel (Ubuntu 24.04 with Python 3, Node.js 22 and git in the default template).

    Like CAMEL's ``DaytonaRuntime``, each tool's function source is sent to the sandbox and called
    there with the model's arguments, and its JSON-able return value comes back. The function must be
    self-contained: a plain function (not a method) that imports what it needs inside its body, with
    literal default values. Its type annotations are dropped in the sandbox (the schema the model sees
    keeps them).

    The sandbox is created by :meth:`build`, or on the first tool call, and is reused by every call.
    :meth:`cleanup` (or ``stop()``, or leaving a ``with`` block) deletes it; one left behind pauses
    itself after ``auto_pause_min`` idle minutes and is deleted ``auto_delete_min`` minutes later.

    Args:
        sandbox_id (Optional[str]): Use this existing sandbox instead of creating one. It is not
            deleted on cleanup, and the options for a new sandbox below do not apply.
        template (str): ``prized-base-v1``, or ``prized-agent-v1`` (adds Claude Code and Codex).
        vcpu, memory_gb, disk_gb: The shape of a new sandbox (template defaults when None).
        env_vars (Optional[Mapping[str, str]]): Environment for a new sandbox, readable by the tools.
        labels (Optional[Mapping[str, str]]): Labels for a new sandbox.
        auto_pause_min (Optional[int]): Idle minutes before a new sandbox pauses itself. (default: 10)
        auto_delete_min (Optional[int]): Minutes after pausing before a new sandbox is deleted.
            (default: 60)
        max_runtime_min (Optional[int]): Running minutes before a new sandbox is stopped.
        timeout (float): Seconds one tool call may run. (default: 120)
        api_key (Optional[str]): A Prized API key with Sandboxes: Write. Defaults to the
            ``PRIZED_API_KEY`` environment variable.
        base_url (Optional[str]): The Prized API origin. (default: https://api.prized.dev)
    """

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
        timeout: float = DEFAULT_TIMEOUT,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        if timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        self.timeout = timeout
        self.entrypoint: dict[str, str] = {}
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

    @property
    def sandbox_id(self) -> str | None:
        """The sandbox's id, once it exists (or the one given to attach to)."""
        return self._holder.sandbox_id

    @property
    def sandbox(self) -> prized.Sandbox | None:
        """The ``prized.Sandbox`` in use (None before :meth:`build`), for anything else in the SDK."""
        return self._holder.sandbox

    def build(self) -> PrizedRuntime:
        r"""Creates (or attaches to) the sandbox now. Also reopens a runtime after :meth:`cleanup`.

        Returns:
            PrizedRuntime: The current runtime.
        """
        self._holder.reopen()
        try:
            sandbox = self._holder.get()
        except PrizedError as err:
            raise RuntimeError(f"Prized sandbox creation failed: {err}") from err
        logger.info("Prized sandbox %s ready", sandbox.id)
        return self

    def add(
        self,
        funcs: FunctionTool | list[FunctionTool],
        entrypoint: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> PrizedRuntime:
        r"""Adds tools whose functions will run in the sandbox.

        Args:
            funcs (Union[FunctionTool, List[FunctionTool]]): The tool or tools to add. Each function
                must be a plain, self-contained function (imports inside its body).
            entrypoint (str): Kept for the same call shape as ``DaytonaRuntime.add``; recorded per
                tool in ``self.entrypoint``, not used to run it.
            arguments (Optional[Dict[str, Any]]): Keyword arguments added to every call (the model's
                own arguments win).

        Returns:
            PrizedRuntime: The current runtime.

        Raises:
            ValueError: If a tool's function is a method, a lambda or has no readable source.
        """
        tools = funcs if isinstance(funcs, list) else [funcs]
        fixed = dict(arguments or {})
        for tool in tools:
            name = tool.get_function_name()
            def_name, source = _function_source(tool.func, name)
            tool.func = self._wrap(tool.func, def_name, source, fixed)
            self.tools_map[name] = tool
            self.entrypoint[name] = entrypoint
        return self

    def _wrap(self, inner: Callable[..., Any], name: str, source: str, fixed: dict[str, Any]) -> Callable[..., Any]:
        @wraps(inner)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            call_kwargs = {**fixed, **kwargs}
            try:
                # Pydantic models (also nested, as CAMEL builds them from the model's arguments),
                # dates and the like become plain JSON.
                args_json = json.dumps(to_jsonable_python(list(args)))
                kwargs_json = json.dumps(to_jsonable_python(call_kwargs))
            except (TypeError, ValueError) as err:
                return {"error": f"Argument serialization failed: {err}"}
            marker = f"@@camel-prized-result-{uuid.uuid4().hex}@@"
            runner = _RUNNER.format(
                marker=marker,
                source=source,
                filename=f"<{name}>",
                name=name,
                args=args_json,
                kwargs=kwargs_json,
            )
            try:
                result, timed_out, _ = self._holder.run_program(
                    ("python3", "-c"), ("python3",), ".py", runner, self.timeout
                )
            except PrizedError as err:
                logger.error("Prized sandbox error running %s: %s", name, err)
                return {"error": f"Execution failed: {err}"}
            if timed_out:
                return {"error": f"Timed out after {self.timeout:g} seconds", "stdout": _tail(result.stdout)}
            _head, found, tail = result.stdout.rpartition("\n" + marker)
            if not found and result.truncated:
                return {"error": "The tool printed over 4 MiB, which cut off its result: have it print less."}
            if not found:
                return {
                    "error": f"Execution failed with return code {result.exit_code}",
                    "stdout": _tail(result.stdout),
                    "stderr": _tail(result.stderr),
                }
            try:
                answer = json.loads(tail)
            except json.JSONDecodeError as err:
                return {"error": f"JSON decoding failed: {err}"}
            return answer["ok"] if "ok" in answer else answer

        return wrapper

    def cleanup(self) -> None:
        r"""Deletes the sandbox if this runtime created it."""
        try:
            self._holder.close()
        except PrizedError as err:
            logger.error("Failed to delete Prized sandbox %s: %s", self._holder.sandbox_id, err)

    def reset(self) -> PrizedRuntime:
        r"""Deletes the sandbox (if this runtime created it) and builds again: a fresh sandbox, or the
        same attached one."""
        self.cleanup()
        return self.build()

    def info(self) -> str:
        r"""A short description of the sandbox.

        Raises:
            RuntimeError: If the sandbox is not built yet.
        """
        sandbox = self._holder.sandbox
        if sandbox is None:
            raise RuntimeError("Sandbox not initialized. Call build() first.")
        sandbox.refresh()
        r = sandbox.resources
        return (
            f"Sandbox {sandbox.id}:\nState: {sandbox.state}\n"
            f"Resources: {r.vcpu} vCPU, {r.memory_gb:g} GB RAM, {r.disk_gb} GB disk"
        )

    @property
    def docs(self) -> str:
        r"""The Prized Sandboxes documentation."""
        return "https://prized.dev/docs/sandboxes?via=camel"

    def __del__(self) -> None:
        if getattr(self, "_holder", None) is not None:
            try:
                self.cleanup()
            except Exception:
                pass


def _tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "..." + text[-limit:]


def _function_source(func: Callable[..., Any], name: str) -> tuple[str, str]:
    """The function's ``def`` name and its source with decorators dropped, ready to exec in the sandbox."""
    if inspect.ismethod(func) or not inspect.isfunction(func):
        raise ValueError(
            f"PrizedRuntime can't run `{name}`: only plain functions can be sent to the sandbox, "
            "not methods or callable objects."
        )
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError, SyntaxError) as err:
        raise ValueError(f"PrizedRuntime can't read the source of `{name}`: {err}") from err
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(defs) != 1 or isinstance(defs[0], ast.AsyncFunctionDef):
        raise ValueError(f"PrizedRuntime can't run `{name}`: it must be a plain `def` (not a lambda or async).")
    node = defs[0]
    node.decorator_list = []
    # Annotations are evaluated when the def runs, and the names they use (List, Optional, a
    # pydantic model) are not defined in the sandbox: drop them.
    node.returns = None
    every = node.args
    for arg in [*every.posonlyargs, *every.args, *every.kwonlyargs, every.vararg, every.kwarg]:
        if arg is not None:
            arg.annotation = None
    return node.name, ast.unparse(ast.Module(body=[node], type_ignores=[]))
