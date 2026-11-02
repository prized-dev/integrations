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

"""Live tests against the real Prized API: they create small sandboxes (1 vCPU, 2 GB), billed to the
workspace of PRIZED_API_KEY, and delete them. Run with ``PRIZED_LIVE=1 pytest -m live -s``."""

from __future__ import annotations

import os
import time

import prized
import pytest
from camel.toolkits import FunctionTool

from camel_prized import PrizedCodeExecutionToolkit, PrizedInterpreter, PrizedRuntime

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("PRIZED_LIVE") != "1" or not os.environ.get("PRIZED_API_KEY"),
        reason="live tests need PRIZED_LIVE=1 and PRIZED_API_KEY",
    ),
]

SHAPE = {"vcpu": 1, "memory_gb": 2, "labels": {"test": "camel-prized-live"}, "max_runtime_min": 15}


def assert_deleted(sandbox_id: str) -> None:
    """The sandbox is gone: the API answers 404 for it (within a minute of the delete)."""
    deadline = time.monotonic() + 60
    while True:
        try:
            sandbox = prized.Sandbox.connect(sandbox_id)
        except prized.PrizedError as err:
            assert err.status == 404, err
            return
        state = sandbox.state
        sandbox.close()
        assert state in ("deleting", "deleted"), state
        assert time.monotonic() < deadline, f"{sandbox_id} still {state}"
        time.sleep(3)


def where_am_i() -> dict:
    r"""Reports where this function runs.

    Returns:
        dict: The home directory, user and kernel.
    """
    import getpass
    import os
    import platform

    return {"home": os.path.expanduser("~"), "user": getpass.getuser(), "kernel": platform.release()}


def add(x: int, y: int) -> int:
    r"""Adds two numbers.

    Args:
        x (int): The first number.
        y (int): The second number.

    Returns:
        int: The sum.
    """
    return x + y


def test_interpreter_live():
    with PrizedInterpreter(timeout=60, **SHAPE) as interp:
        assert interp.run("import sys; print(sys.version_info[0], 6 * 7)") == "3 42\n"
        assert interp.run("echo $((40 + 2)) $(whoami) $PWD", "bash") == "42 prized /home/prized/work\n"
        assert interp.run("console.log([1, 2, 3].map(x => x * 2).join(','))", "javascript") == "2,4,6\n"
        out = interp.execute_command("pip install --quiet --disable-pip-version-check tabulate==0.9.0")
        assert "Execution failed" not in out, out
        assert interp.run("import tabulate; print(tabulate.__version__)") == "0.9.0\n"
        assert interp.run("import sys; print('oops', file=sys.stderr); sys.exit(3)").endswith(
            "(stderr: oops\n)(Execution failed with return code 3)"
        )
        big = "# " + "x" * 100_000 + "\nprint('big ok')"
        assert interp.run(big) == "big ok\n"
        assert interp.execute_command("ls -A /home/prized/.camel") == "Code executed successfully (no output)."
        sandbox_id = interp.sandbox_id
        assert sandbox_id
    with PrizedInterpreter(timeout=5, **SHAPE) as interp:
        out = interp.run("import time; print('started', flush=True); time.sleep(30)")
        assert out.startswith("started\n") and out.endswith("(Execution timed out after 5 seconds and was stopped.)")
        second_id = interp.sandbox_id
    assert_deleted(sandbox_id)
    assert_deleted(second_id or "")


def test_runtime_live():
    tools = [FunctionTool(where_am_i), FunctionTool(add)]
    with PrizedRuntime(**SHAPE) as runtime:
        runtime.add(tools, "live").build()
        sandbox_id = runtime.sandbox_id
        here = tools[0]()
        assert here["home"] == "/home/prized" and here["user"] == "prized", here
        assert tools[1](x=40, y=2) == 42
        assert "1 vCPU" in runtime.info()
    assert_deleted(sandbox_id or "")


def test_toolkit_live():
    with PrizedCodeExecutionToolkit(**SHAPE) as toolkit:
        execute_code = {t.get_function_name(): t for t in toolkit.get_tools()}["execute_code"]
        out = execute_code(code="print(sum(range(101)))", code_type="python")
        assert "> Executed Results:\n5050\n" in out, out
        sandbox_id = toolkit.sandbox_id
    assert_deleted(sandbox_id or "")
