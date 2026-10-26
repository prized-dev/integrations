"""A real `Agent` with `PrizedSandbox` and Harness's `Coder`, in a live sandbox.

The model is scripted (`FunctionModel`), so no LLM key is needed: it writes a file, runs it, and a
second run continues in the same sandbox by ref. Needs PRIZED_API_KEY; the sandbox is deleted at the end.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.workspaces import WorkspaceUnavailableError

from pydantic_ai_prized import PrizedSandbox

pytestmark = pytest.mark.skipif(not os.environ.get('PRIZED_API_KEY'), reason='needs PRIZED_API_KEY')

pytest.importorskip('pydantic_ai_harness.coder')
from pydantic_ai_harness.coder import Coder  # noqa: E402

LABELS = {
    'pydantic-ai-prized-test': uuid.uuid4().hex[:12],
    **dict(item.split('=', 1) for item in os.environ.get('PRIZED_TEST_LABELS', '').split(',') if '=' in item),
}


def _returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        part for m in messages if isinstance(m, ModelRequest) for part in m.parts if isinstance(part, ToolReturnPart)
    ]


def scripted(*calls: tuple[str, dict[str, str]]):
    """A streamed model that makes `calls` in order, then answers with the last tool output."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        step = len(_returns(messages))  # each run here starts without message history
        if step < len(calls):
            name, args = calls[step]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f'call-{step}')}
        else:
            yield str(_returns(messages)[-1].content)

    return FunctionModel(stream_function=stream)


def test_coder_agent_runs_and_continues_in_a_prized_sandbox() -> None:
    sandbox = PrizedSandbox(labels=LABELS, vcpu=1, memory_mb=2048, max_runtime_min=15)
    first_agent = Agent(
        scripted(
            ('write_file', {'path': 'answer.py', 'content': 'print(6 * 7)\n'}),
            ('shell', {'command': 'python3 answer.py'}),
        ),
        capabilities=[sandbox, Coder()],
    )
    first = first_agent.run_sync('Write answer.py and run it.')
    ref = first.workspace.ref
    assert ref is not None and ref.provider == 'prized' and ref.id.startswith('sbx_')
    try:
        assert '42' in first.output

        # A new conversation reaches the same sandbox by ref: the file is still there.
        second_agent = Agent(scripted(('shell', {'command': 'cat answer.py && pwd'})), capabilities=[sandbox, Coder()])
        second = second_agent.run_sync('Show answer.py.', workspace=ref)
        assert second.workspace.ref == ref
        assert 'print(6 * 7)' in second.output
        assert '/home/prized/work' in second.output
    finally:
        asyncio.run(sandbox.destroy(ref))

    # Once deleted, the ref is refused instead of quietly starting over in an empty sandbox.
    third_agent = Agent(scripted(('shell', {'command': 'true'})), capabilities=[sandbox, Coder()])
    with pytest.raises(WorkspaceUnavailableError):
        third_agent.run_sync('Anything.', workspace=ref)
