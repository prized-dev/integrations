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

"""PrizedCodeExecutionToolkit: CAMEL's code-execution tools, running in the fake sandbox."""

from __future__ import annotations

import pytest
from camel.toolkits import CodeExecutionToolkit

from camel_prized import PrizedCodeExecutionToolkit, PrizedInterpreter


def test_tools_run_in_the_sandbox(cloud):
    toolkit = PrizedCodeExecutionToolkit()
    assert isinstance(toolkit, CodeExecutionToolkit)
    tools = {t.get_function_name(): t for t in toolkit.get_tools()}
    assert set(tools) == {"execute_code", "execute_command"}
    out = tools["execute_code"](code="print(2 ** 10)", code_type="python")
    assert "> Executed Results:\n1024\n" in out
    out = tools["execute_command"](command="echo ready")
    assert "> Executed Results:\nready\n" in out
    assert len(cloud.created) == 1
    toolkit.close()
    assert cloud.created[0].deletes == 1


def test_options_reach_the_interpreter(cloud):
    toolkit = PrizedCodeExecutionToolkit(timeout=7, template="prized-agent-v1", vcpu=4)
    assert isinstance(toolkit.interpreter, PrizedInterpreter)
    assert toolkit.interpreter.timeout == 7
    assert toolkit.timeout is None  # no thread timeout on top: the sandbox stops the command itself
    toolkit.execute_code("print(1)")
    assert cloud.create_calls[0]["template"] == "prized-agent-v1" and cloud.create_calls[0]["vcpu"] == 4
    assert cloud.created[0].timeouts == [7]
    assert toolkit.sandbox_id == cloud.created[0].id


def test_bring_your_own_interpreter(cloud):
    interp = PrizedInterpreter()
    with PrizedCodeExecutionToolkit(interpreter=interp) as toolkit:
        assert toolkit.interpreter is interp
        toolkit.execute_code("print(1)")
    assert cloud.created[0].deletes == 1
    with pytest.raises(ValueError, match="not both"):
        PrizedCodeExecutionToolkit(interpreter=interp, vcpu=2)


def test_nothing_is_created_until_code_runs(cloud):
    PrizedCodeExecutionToolkit().get_tools()
    assert cloud.create_calls == []
