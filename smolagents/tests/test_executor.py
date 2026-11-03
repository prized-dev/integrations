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

"""Offline tests: a real kernel on a real unix socket, inside a fake sandbox that runs bash locally.

They cover the smolagents ``PythonExecutor`` contract (run, persistent state, injected variables,
shell lines, the final-answer protocol and errors) plus the sandbox lifecycle (create, attach,
cleanup), and finish with a full ``CodeAgent`` run driven by a scripted model.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from prized import ExecResult
from smolagents.default_tools import FinalAnswerTool
from smolagents.utils import AgentError

from smolagents_prized import PrizedExecutor


@pytest.fixture
def executor(cloud) -> Iterator[PrizedExecutor]:
    ex = PrizedExecutor()
    try:
        yield ex
    finally:
        with contextlib.suppress(Exception):
            ex.cleanup()


def test_create_uses_template_and_labels(cloud):
    ex = PrizedExecutor(template="prized-base-v1")
    try:
        assert len(cloud.created) == 1
        call = cloud.create_calls[0]
        assert call["template"] == "prized-base-v1"
        assert call["labels"]["created-by"] == "smolagents"
        assert ex.sandbox_id.startswith("sbx_fake")
    finally:
        ex.cleanup()


def test_installs_numpy_and_pillow_when_missing(cloud, monkeypatch):
    # On a bare template where numpy/PIL aren't importable, the executor installs them, because
    # smolagents' tool code imports them at module level. We make the import probe report "missing"
    # and record install_packages (so the offline test never reaches the network).
    import prized

    calls: list[list[str]] = []

    def record_install(self, packages):
        calls.append(list(packages))
        return list(packages)

    monkeypatch.setattr(PrizedExecutor, "install_packages", record_install)

    real_create = cloud.create

    def create_with_missing_deps(template="prized-base-v1", **kwargs):
        sandbox = real_create(template, **kwargs)
        sandbox.exec_hook = lambda cmd: ExecResult("", "ModuleNotFoundError", 1) if "import numpy, PIL" in cmd else None
        return sandbox

    monkeypatch.setattr(prized.Sandbox, "create", create_with_missing_deps)

    ex = PrizedExecutor()
    try:
        assert ["numpy", "pillow"] in calls
    finally:
        ex.cleanup()


def test_runs_code_and_captures_logs(executor):
    out = executor.run_code_raise_errors("print('hello from prized')\n2 + 3")
    assert "hello from prized" in out.logs
    assert out.output == "5"
    assert out.is_final_answer is False


def test_no_trailing_expression_gives_no_output(executor):
    out = executor.run_code_raise_errors("x = 1 + 1")
    assert out.output is None
    assert out.is_final_answer is False


def test_state_persists_across_cells(executor):
    executor.run_code_raise_errors("import math\nvalue = math.factorial(5)")
    out = executor.run_code_raise_errors("value * 2")
    assert out.output == "240"


def test_send_variables_then_use_them(executor):
    executor.send_variables({"given": 41})
    out = executor.run_code_raise_errors("given + 1")
    assert out.output == "42"


def test_shell_escape_line_runs(executor):
    out = executor.run_code_raise_errors("!echo shelled-out")
    assert "shelled-out" in out.logs


def test_python_error_raises_agent_error(executor):
    with pytest.raises(AgentError) as info:
        executor.run_code_raise_errors("print('before the boom')\nraise ValueError('boom')")
    message = str(info.value)
    assert "ValueError" in message
    assert "boom" in message
    assert "before the boom" in message  # logs up to the error are kept


def test_final_answer_protocol(executor):
    # send_tools patches FinalAnswerTool so that final_answer(x) raises inside the kernel; the
    # executor must catch it, deserialize the payload and report it as the final answer.
    executor.send_tools({"final_answer": FinalAnswerTool()})
    out = executor.run_code_raise_errors("final_answer('the one true answer')")
    assert out.is_final_answer is True
    assert out.output == "the one true answer"


def test_final_answer_roundtrips_a_structure(executor):
    executor.send_tools({"final_answer": FinalAnswerTool()})
    out = executor.run_code_raise_errors("final_answer({'n': 42, 'items': [1, 2, 3]})")
    assert out.is_final_answer is True
    assert out.output == {"n": 42, "items": [1, 2, 3]}


def test_cleanup_deletes_owned_sandbox(cloud):
    ex = PrizedExecutor()
    sandbox = cloud.created[0]
    ex.cleanup()
    assert sandbox.deletes == 1
    assert sandbox.state == "deleted"
    ex.cleanup()  # idempotent
    assert sandbox.deletes == 1


def test_attached_sandbox_is_not_deleted(cloud):
    existing = cloud.add_existing()
    ex = PrizedExecutor(sandbox_id=existing.id)
    assert cloud.connect_calls[0]["sandbox_id"] == existing.id
    ex.cleanup()
    assert existing.deletes == 0


def test_garbled_kernel_reply_is_reported(executor):
    # If the client prints nothing parseable (e.g. the kernel died), say so instead of crashing.
    executor.sandbox.exec_hook = lambda cmd: ExecResult("not json", "segfault", 1) if "_client.py" in cmd else None
    with pytest.raises(AgentError) as info:
        executor.run_code_raise_errors("1 + 1")
    assert "did not return a result" in str(info.value)


def test_full_code_agent_run(cloud):
    from smolagents import ChatMessage, CodeAgent, MessageRole

    class ScriptedModel:
        """A model that answers with one code blob calling final_answer."""

        def __init__(self) -> None:
            self.model_id = "scripted"

        def generate(self, messages, stop_sequences=None, **kwargs):
            code = "result = 6 * 7\nfinal_answer(result)"
            content = f"Thought: compute it.\nCode:\n```py\n{code}\n```<end_code>"
            return ChatMessage(role=MessageRole.ASSISTANT, content=content)

        __call__ = generate

    agent = CodeAgent(tools=[], model=ScriptedModel(), executor=PrizedExecutor())
    try:
        result = agent.run("What is 6 times 7?")
        assert str(result) == "42"
    finally:
        agent.cleanup()
