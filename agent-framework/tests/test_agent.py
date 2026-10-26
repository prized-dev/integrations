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

"""PrizedShellTool inside real Agent Framework agents, driven by a scripted chat client."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from agent_framework import (
    Agent,
    AgentSession,
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    FunctionTool,
    Message,
    create_harness_agent,
)

from agent_framework_prized import PrizedShellTool
from tests.fakes import FakeSdk


class ScriptedClient(FunctionInvocationLayer, BaseChatClient):
    """A model that calls the shell tool with each scripted command in turn, then stops.
    It records what it was told: the function results and its instructions."""

    def __init__(self, commands: Sequence[Any], tool_name: str = "run_shell") -> None:
        super().__init__()
        self.commands = list(commands)
        self.tool_name = tool_name
        self.results: list[str] = []
        self.instructions: list[str] = []
        self.calls = 0

    @staticmethod
    def get_shell_tool(*, func: Any = None, **_: Any) -> Any:  # SupportsShellTool
        assert isinstance(func, FunctionTool)
        return func

    def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **_: Any):
        assert not stream

        async def respond() -> ChatResponse:
            self.instructions.append(str(options.get("instructions") or ""))
            self.results = [str(c.result) for m in messages for c in m.contents if c.type == "function_result"]
            if self.commands:
                self.calls += 1
                step = self.commands.pop(0)  # a shell command, or (tool name, arguments)
                name, arguments = (self.tool_name, {"command": step}) if isinstance(step, str) else step
                call = Content.from_function_call(call_id=f"call{self.calls}", name=name, arguments=arguments)
                return ChatResponse(messages=[Message(role="assistant", contents=[call])])
            return ChatResponse(messages=[Message(role="assistant", contents=["done"])])

        return respond()


@pytest.fixture
def sdk():
    fake = FakeSdk()
    with fake.patch():
        yield fake


async def test_agent_runs_the_models_commands_in_the_sandbox(sdk):
    client = ScriptedClient(["mkdir proj && cd proj && echo 'print(40 + 2)' > m.py", "python3 m.py; pwd"])
    async with PrizedShellTool(approval_mode="never_require") as shell:
        agent = Agent(client=client, tools=[shell.as_function()])
        response = await agent.run("compute")
        assert response.text == "done"
        assert "42" in client.results[-1] and "/home/prized/work/proj" in client.results[-1]
        assert "exit_code: 0" in client.results[-1]
    assert sdk.alive() == []


async def test_approval_is_required_by_default(sdk):
    client = ScriptedClient(["echo should-not-run"])
    async with PrizedShellTool() as shell:
        agent = Agent(client=client, tools=[shell.as_function()])
        response = await agent.run("go")
        assert response.user_input_requests, "the call should wait for approval"
        assert not [c for c in sdk.created[0].calls if "should-not-run" in str(c[0])]
    assert sdk.alive() == []


async def test_harness_agent_takes_it_as_its_shell_executor(sdk):
    client = ScriptedClient(["uname -s; whoami"])
    async with PrizedShellTool(approval_mode="never_require") as shell:
        agent = create_harness_agent(
            client,
            shell_executor=shell,
            shell_environment_provider_options=shell.environment_options(probe_tools=("git",)),
            disable_file_memory=True,
            disable_web_search=True,
        )
        response = await agent.run("where are you?", session=AgentSession())
        assert response.text == "done"
        assert "Linux" in client.results[-1]
        system = client.instructions[0]
        assert "Prized sandbox" in system and "Working directory: /home/prized/work" in system
    assert sdk.alive() == []
