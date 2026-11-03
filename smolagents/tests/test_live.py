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

"""Live tests against the real Prized API. They create a real sandbox, billed to your workspace, and
delete it at the end.

    PRIZED_LIVE=1 PRIZED_API_KEY=dcp_... pytest -m live

They don't call a language model: they drive the executor the way a CodeAgent does, so they check the
whole sandbox path (start the kernel, run cells, keep state across a pause/resume, the final-answer
protocol, cleanup) without an API key for a model.
"""

from __future__ import annotations

import os

import pytest
from prized import PrizedError
from smolagents.default_tools import FinalAnswerTool

from smolagents_prized import PrizedExecutor

pytestmark = pytest.mark.live

if not os.environ.get("PRIZED_LIVE"):
    pytest.skip("set PRIZED_LIVE=1 (and PRIZED_API_KEY) to run live tests", allow_module_level=True)


@pytest.fixture
def executor():
    ex = PrizedExecutor(labels={"test": "smolagents-live"})
    try:
        yield ex
    finally:
        ex.cleanup()


def test_runs_code_and_keeps_state(executor):
    first = executor.run_code_raise_errors("import platform\nprint(platform.system())\nvalues = [1, 2, 3]")
    assert "Linux" in first.logs
    second = executor.run_code_raise_errors("sum(values)")
    assert second.output == "6"
    assert second.is_final_answer is False


def test_state_survives_a_pause_resume(executor):
    # The point of a Prized sandbox: it pauses with its memory, so the kernel's namespace is still
    # there after a resume.
    executor.run_code_raise_errors("kept = 'still here'")
    executor.sandbox.pause()
    executor.sandbox.resume()
    out = executor.run_code_raise_errors("kept")
    assert out.output == "'still here'"


def test_final_answer(executor):
    executor.send_tools({"final_answer": FinalAnswerTool()})
    out = executor.run_code_raise_errors("final_answer({'result': 42})")
    assert out.is_final_answer is True
    assert out.output == {"result": 42}


def test_cleanup_deletes_the_sandbox():
    ex = PrizedExecutor(labels={"test": "smolagents-live"})
    sandbox_id = ex.sandbox_id
    ex.cleanup()
    with pytest.raises(PrizedError):
        # The sandbox is gone; connecting to it should fail.
        PrizedExecutor(sandbox_id=sandbox_id)
