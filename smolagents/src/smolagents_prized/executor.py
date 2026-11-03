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

"""A smolagents ``PythonExecutor`` that runs the agent's code in a Prized sandbox."""

from __future__ import annotations

import contextlib
import importlib.resources
import json
import os
import secrets
import shlex
import time
from collections.abc import Mapping
from typing import Any

import prized
from prized import CommandTimeoutError, ExecResult, PrizedError
from smolagents.local_python_executor import CodeOutput
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.remote_executors import RemotePythonExecutor
from smolagents.utils import AgentError

DEFAULT_TEMPLATE = "prized-base-v1"
# The sandbox pauses itself after this many idle minutes (its memory, and this kernel, are kept), and
# is deleted after this many idle minutes, so a leaked executor cannot run up a bill forever.
DEFAULT_AUTO_PAUSE_MIN = 10
DEFAULT_AUTO_DELETE_MIN = 60
DEFAULT_STEP_TIMEOUT = 300.0
DEFAULT_STARTUP_TIMEOUT = 90.0
HOME = "/home/prized"
LABELS = {"created-by": "smolagents"}
# smolagents' final-answer serializer (sent to the sandbox with every CodeAgent's tools) imports
# numpy and PIL at module level, so the sandbox needs both. We install them only if the template
# doesn't already have them. Import name -> pip name.
_SERIALIZATION_DEPS = {"numpy": "numpy", "PIL": "pillow"}
API_KEYS_URL = "https://prized.dev/dashboard/workspace/api-keys?via=smolagents"
# The template Python is "externally managed"; this lets `pip install` put packages in ~/.local.
_COMMAND_PREFIX = 'export PATH="$HOME/.local/bin:$PATH" PIP_BREAK_SYSTEM_PACKAGES="${PIP_BREAK_SYSTEM_PACKAGES-1}"; '


def resolve_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("PRIZED_API_KEY")
    if not key:
        raise ValueError(
            "Prized needs an API key: pass api_key= or set PRIZED_API_KEY. "
            f"Make one with Sandboxes: Write access at {API_KEYS_URL}"
        )
    return key


def _script(name: str) -> str:
    """The text of a bundled in-sandbox script (_kernel.py / _client.py)."""
    return importlib.resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


class PrizedExecutor(RemotePythonExecutor):
    """Run a smolagents ``CodeAgent``'s Python in a Prized sandbox (a Firecracker microVM).

    Pass an instance as ``CodeAgent(..., executor=PrizedExecutor())``. One persistent Python kernel
    lives in the sandbox and holds the agent's namespace in memory across steps; because the sandbox
    pauses *with its memory*, that namespace also survives the pause between the agent's turns.

    Args:
        additional_imports: Extra pip packages to install in the sandbox before the run.
        logger: A smolagents ``AgentLogger``. One at ``LogLevel.INFO`` is made if omitted.
        allow_pickle: Passed through to :class:`RemotePythonExecutor` (JSON-only by default).
        api_key: Prized API key. Falls back to ``PRIZED_API_KEY``.
        base_url: Override the Prized API base URL (for self-hosted or staging).
        template: Sandbox template to start from.
        sandbox_id: Attach to an existing sandbox instead of creating one (then it is not deleted
            on :meth:`cleanup`).
        vcpu, memory_gb, disk_gb: Sandbox size.
        env_vars: Environment variables set in the sandbox.
        labels: Extra labels on the sandbox.
        auto_pause_min, auto_delete_min, max_runtime_min: Sandbox lifecycle, in minutes.
        step_timeout: Seconds a single code cell may run before it is stopped.
        startup_timeout: Seconds to wait for the kernel to come up.
    """

    def __init__(
        self,
        additional_imports: list[str] | None = None,
        logger: AgentLogger | None = None,
        allow_pickle: bool = False,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        template: str = DEFAULT_TEMPLATE,
        sandbox_id: str | None = None,
        vcpu: int | None = None,
        memory_gb: float | None = None,
        disk_gb: int | None = None,
        env_vars: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
        auto_pause_min: int | None = DEFAULT_AUTO_PAUSE_MIN,
        auto_delete_min: int | None = DEFAULT_AUTO_DELETE_MIN,
        max_runtime_min: int | None = None,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        super().__init__(additional_imports or [], logger or AgentLogger(level=LogLevel.INFO), allow_pickle)
        self._api_key = resolve_api_key(api_key)
        self._base_url = base_url
        self._owned = sandbox_id is None
        self._step_timeout = step_timeout
        self._closed = False
        self._counter = 0

        token = secrets.token_hex(4)
        self._dir = f"{HOME}/.smolk-{token}"
        self._kernel_py = f"{self._dir}/_kernel.py"
        self._client_py = f"{self._dir}/_client.py"
        self._sock = f"{self._dir}/k.sock"
        self._ready = f"{self._dir}/ready"
        self._log = f"{self._dir}/kernel.log"

        if sandbox_id is not None:
            self.sandbox = prized.Sandbox.connect(sandbox_id, api_key=self._api_key, base_url=self._base_url)
        else:
            self.sandbox = prized.Sandbox.create(
                template,
                api_key=self._api_key,
                base_url=self._base_url,
                vcpu=vcpu,
                memory_gb=memory_gb,
                disk_gb=disk_gb,
                env_vars=dict(env_vars) if env_vars else None,
                labels={**LABELS, **dict(labels or {})},
                auto_pause_min=auto_pause_min,
                auto_delete_min=auto_delete_min,
                max_runtime_min=max_runtime_min,
            )
        self.logger.log(f"Prized sandbox {self.sandbox.id} is up, starting the kernel...", level=LogLevel.INFO)
        self._start_kernel(startup_timeout)
        self._ensure_serialization_deps()
        self.installed_packages += self.install_packages(self.additional_imports)

    def _ensure_serialization_deps(self) -> None:
        """Make sure numpy and PIL (pillow) are importable in the sandbox, installing only what's
        missing. smolagents' tool-definition code imports them at module level, so without them the
        first ``send_tools`` would fail."""
        import_names = ", ".join(_SERIALIZATION_DEPS)  # "numpy, PIL"
        probe = self.sandbox.exec(f"{_COMMAND_PREFIX}python3 -c 'import {import_names}'", timeout=60)
        if probe.exit_code != 0:
            self.logger.log("Installing numpy and pillow for smolagents' serialization...", level=LogLevel.INFO)
            self.install_packages(list(_SERIALIZATION_DEPS.values()))
        # Mark them as present so send_tools doesn't try to reinstall them later.
        for pip_name in _SERIALIZATION_DEPS.values():
            if pip_name not in self.installed_packages:
                self.installed_packages.append(pip_name)

    @property
    def sandbox_id(self) -> str:
        return self.sandbox.id

    def _start_kernel(self, startup_timeout: float) -> None:
        self.sandbox.files.write(self._kernel_py, _script("_kernel.py"))
        self.sandbox.files.write(self._client_py, _script("_client.py"))
        # Detach the kernel (its own session, stdio off the exec's pipes) so this command returns while
        # the kernel keeps running and serving later steps. Env and paths are inline so they hold on a
        # real sandbox and under the offline test's bash alike.
        launch = (
            f"{_COMMAND_PREFIX}"
            f"export SMOL_KERNEL_SOCK={shlex.quote(self._sock)} SMOL_KERNEL_READY={shlex.quote(self._ready)}; "
            f"setsid python3 {shlex.quote(self._kernel_py)} >{shlex.quote(self._log)} 2>&1 </dev/null & echo started"
        )
        self.sandbox.exec(launch, timeout=60)

        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if self.sandbox.files.exists(self._ready):
                self.logger.log("Prized kernel is ready.", level=LogLevel.INFO)
                return
            time.sleep(0.25)
        log_tail = self._read_log()
        raise RuntimeError(f"Prized kernel did not start within {startup_timeout:g}s.{log_tail}")

    def _read_log(self) -> str:
        try:
            return "\nKernel log:\n" + self.sandbox.files.read(self._log)
        except PrizedError:
            return ""

    def run_code_raise_errors(self, code: str) -> CodeOutput:
        if self._closed:
            raise RuntimeError("This PrizedExecutor has been cleaned up; make a new one.")
        self._counter += 1
        req_path = f"{self._dir}/req-{self._counter}.py"
        self.sandbox.files.write(req_path, code)
        command = (
            f"export SMOL_KERNEL_SOCK={shlex.quote(self._sock)}; "
            f"python3 {shlex.quote(self._client_py)} {shlex.quote(req_path)}"
        )
        try:
            result = self.sandbox.exec(command, timeout=self._step_timeout)
        except CommandTimeoutError as err:
            raise AgentError(
                f"{err.stdout or ''}\nExecution timed out after {self._step_timeout:g} seconds and was stopped.",
                self.logger,
            ) from err
        finally:
            with contextlib.suppress(PrizedError):
                self.sandbox.files.remove(req_path)

        reply = self._parse_reply(result)
        logs = reply.get("logs", "") or ""
        error = reply.get("error")
        if error:
            raise AgentError(
                f"{logs}\nExecuting code yielded an error:\n{error['name']}\n{error['value']}\n{error['traceback']}",
                self.logger,
            )
        if reply.get("is_final_answer"):
            output = self._deserialize_final_answer(reply["result"], self.allow_pickle)
            return CodeOutput(output=output, logs=logs, is_final_answer=True)
        return CodeOutput(output=reply.get("result"), logs=logs, is_final_answer=False)

    def _parse_reply(self, result: ExecResult) -> dict[str, Any]:
        try:
            reply: dict[str, Any] = json.loads(result.stdout)
            return reply
        except (ValueError, TypeError) as err:
            detail = (result.stderr or "").strip()
            raise AgentError(
                "The Prized kernel did not return a result"
                f"{f' (exit {result.exit_code})' if result.exit_code else ''}"
                f"{f': {detail}' if detail else '.'}"
                f"{self._read_log()}",
                self.logger,
            ) from err

    def cleanup(self) -> None:
        """Stop the kernel and delete the sandbox (only if this executor created it). Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self.sandbox.exec(
                f"export SMOL_KERNEL_SOCK={shlex.quote(self._sock)}; python3 -c {shlex.quote(_SHUTDOWN_SNIPPET)}",
                timeout=15,
            )
        try:
            if self._owned:
                self.sandbox.delete()
                self.logger.log(f"Deleted Prized sandbox {self.sandbox.id}.", level=LogLevel.INFO)
        finally:
            with contextlib.suppress(Exception):
                self.sandbox.close()

    def __del__(self) -> None:  # best effort if the caller forgets cleanup()
        with contextlib.suppress(Exception):
            self.cleanup()


# Ask the kernel to exit (so the sandbox has no stray process before it is deleted). Kept tiny and
# dependency-free so it runs under the sandbox's own Python.
_SHUTDOWN_SNIPPET = (
    "import os,socket,struct,json;"
    "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.connect(os.environ['SMOL_KERNEL_SOCK']);"
    "p=json.dumps({'op':'shutdown'}).encode();s.sendall(struct.pack('>I',len(p))+p);s.close()"
)
