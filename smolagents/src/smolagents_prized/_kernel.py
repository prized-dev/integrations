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

"""The persistent Python kernel that runs inside a Prized sandbox.

smolagents sends the agent's tools and variables once, then runs many code cells that rely on that
state still being there. So the executor keeps one long-lived Python process (this script) in the
sandbox, holding a single namespace in memory, and sends it one code cell per step over a unix
socket. Because a Prized sandbox pauses *with its memory*, the namespace (imports, variables, tool
definitions) survives the pause between the agent's turns.

It speaks a tiny length-prefixed JSON protocol on an ``AF_UNIX`` socket (no network port is opened):
a request ``{"op": "run", "code": "..."}`` gets back ``{"logs", "result", "is_final_answer",
"error"}``. ``result`` is the serialized final-answer payload when ``is_final_answer`` is true (the
executor deserializes it), otherwise the ``repr`` of the cell's last expression, or null.

It has no third-party dependencies: the sandbox's own Python runs it as-is.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import io
import json
import os
import site
import socket
import struct
import subprocess
import sys
import traceback
from typing import Any

# A line that starts with "!" is a shell command, the way a Jupyter kernel treats it. smolagents'
# package installs arrive as "!pip install ...". ~/.local/bin is on PATH and PIP_BREAK_SYSTEM_PACKAGES
# is set so pip can install for the user on the "externally managed" template Python.
_SHELL_PREFIX = 'export PATH="$HOME/.local/bin:$PATH" PIP_BREAK_SYSTEM_PACKAGES="${PIP_BREAK_SYSTEM_PACKAGES-1}"; '


def _shell(command: str) -> int:
    """Run a shell command, streaming its output into the cell's captured logs (IPython's ``!cmd``)."""
    proc = subprocess.run(
        ["bash", "-c", _SHELL_PREFIX + command],
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    _refresh_import_paths()
    return proc.returncode


def _refresh_import_paths() -> None:
    """Make a package installed just now (usually by ``!pip install``) importable in the next cell.

    pip installs into the user site-packages, which this long-lived process may have cached as
    missing at startup. Re-add that directory and drop the stale path caches so the next ``import``
    sees it.
    """
    user_site = site.getusersitepackages()
    if user_site and os.path.isdir(user_site):
        sys.path_importer_cache.pop(user_site, None)
        site.addsitedir(user_site)
    importlib.invalidate_caches()


def _rewrite_shell_lines(code: str) -> str:
    """Turn lines that start with ``!`` into ``_smol_shell("...")`` calls, leaving Python untouched."""
    out = []
    for line in code.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("!"):
            indent = line[: len(line) - len(stripped)]
            out.append(f"{indent}_smol_shell({json.dumps(stripped[1:].strip())})")
        else:
            out.append(line)
    return "\n".join(out)


def _parse_cell(code: str) -> ast.Module:
    """Parse a cell, handling notebook-style ``!shell`` lines.

    Valid Python is parsed as-is, so a ``!`` inside a string or an expression is never touched. Only
    when the cell isn't valid Python (e.g. it has a ``!pip install ...`` line, which is a syntax
    error) do we rewrite leading-``!`` lines to shell calls and parse again.
    """
    try:
        return ast.parse(code, mode="exec")
    except SyntaxError:
        return ast.parse(_rewrite_shell_lines(code), mode="exec")


class Kernel:
    """One persistent namespace. ``run`` executes a cell in it and reports back the way smolagents'
    remote executors do: stdout as logs, a ``FinalAnswerException`` as the final answer, any other
    exception as an error, and the last expression's value (as ``repr``) otherwise."""

    def __init__(self) -> None:
        # A single dict as both globals and locals gives module-level semantics: assignments,
        # imports and ``def``s persist across cells, and ``locals().update(...)`` (how smolagents
        # injects variables) lands in the same namespace.
        self.namespace: dict[str, Any] = {"__name__": "__main__", "_smol_shell": _shell}

    def run(self, code: str) -> dict[str, Any]:
        buffer = io.StringIO()
        result: Any = None
        is_final_answer = False
        error: dict[str, str] | None = None
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                tree = _parse_cell(code)
                last_expr: ast.Expression | None = None
                last_stmt = tree.body[-1] if tree.body else None
                if isinstance(last_stmt, ast.Expr):
                    tree.body.pop()
                    last_expr = ast.Expression(last_stmt.value)
                    ast.copy_location(last_expr, last_stmt)
                exec(compile(tree, "<cell>", "exec"), self.namespace)
                if last_expr is not None:
                    value = eval(compile(last_expr, "<cell>", "eval"), self.namespace)
                    if value is not None:
                        result = repr(value)
            except BaseException as exc:  # a cell may raise anything, including the final-answer sentinel
                if type(exc).__name__ == "FinalAnswerException":
                    is_final_answer = True
                    result = getattr(exc, "value", None)
                else:
                    error = {
                        "name": type(exc).__name__,
                        "value": str(exc),
                        "traceback": traceback.format_exc(),
                    }
        return {
            "logs": buffer.getvalue(),
            "result": result,
            "is_final_answer": is_final_answer,
            "error": error,
        }


def _recv_all(conn: socket.socket, n: int) -> bytes:
    chunks = []
    got = 0
    while got < n:
        chunk = conn.recv(n - got)
        if not chunk:
            raise EOFError("connection closed mid-message")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _recv(conn: socket.socket) -> dict[str, Any]:
    (length,) = struct.unpack(">I", _recv_all(conn, 4))
    request: dict[str, Any] = json.loads(_recv_all(conn, length).decode("utf-8"))
    return request


def _send(conn: socket.socket, obj: dict[str, Any]) -> None:
    data = json.dumps(obj).encode("utf-8")
    conn.sendall(struct.pack(">I", len(data)) + data)


def main() -> None:
    sock_path = os.environ["SMOL_KERNEL_SOCK"]
    ready_path = os.environ["SMOL_KERNEL_READY"]
    with contextlib.suppress(FileNotFoundError):
        os.remove(sock_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(8)
    # Tell the executor the kernel is up only once the socket is accepting connections.
    with open(ready_path, "w", encoding="utf-8") as handle:
        handle.write("ok")

    kernel = Kernel()
    while True:
        conn, _ = server.accept()
        try:
            request = _recv(conn)
            op = request.get("op")
            if op == "shutdown":
                _send(conn, {"ok": True})
                conn.close()
                break
            _send(conn, kernel.run(request["code"]))
        except Exception as exc:  # never let one bad request kill the kernel
            with contextlib.suppress(Exception):
                _send(
                    conn,
                    {
                        "logs": "",
                        "result": None,
                        "is_final_answer": False,
                        "error": {"name": type(exc).__name__, "value": str(exc), "traceback": traceback.format_exc()},
                    },
                )
        finally:
            with contextlib.suppress(Exception):
                conn.close()


if __name__ == "__main__":
    main()
