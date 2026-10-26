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

"""Live tests against the real Prized API.

They create real sandboxes (billed to the key's workspace) and delete every
one. Run with: PRIZED_API_KEY=dcp_... PRIZED_LIVE=1 pytest -m live
"""

import asyncio
import base64
import os
import time
from typing import AsyncGenerator
from unittest import mock
import uuid

from google.adk.agents import LlmAgent
from google.adk.code_executors.code_execution_utils import CodeExecutionInput
from google.adk.code_executors.code_execution_utils import File
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools.environment import EnvironmentToolset
from google.genai import types
import prized
from prized import AsyncSandbox
import pytest

from prized_adk import PrizedCodeExecutor
from prized_adk import PrizedEnvironment

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (
            os.environ.get("PRIZED_API_KEY") and os.environ.get("PRIZED_LIVE")
        ),
        reason="set PRIZED_API_KEY and PRIZED_LIVE=1 to run live tests",
    ),
]

RUN = uuid.uuid4().hex[:8]
LABELS = {"test": "prized-adk-live", "run": RUN}
# Small shapes: forks run 3 at once.
SMALL = {"vcpu": 1, "memory_gb": 2}
CREATED: list[str] = []


def _log(*parts):
  print("[live]", *parts, flush=True)


async def _state(sandbox_id: str) -> str:
  try:
    sb = await AsyncSandbox.connect(sandbox_id)
  except prized.PrizedError as e:
    if e.code == "not_found":
      return "gone"
    raise
  try:
    return sb.state
  finally:
    await sb.close()


@pytest.fixture(scope="module", autouse=True)
def _sweep():
  """Delete anything this run labeled, whatever the tests did."""
  yield

  async def sweep():
    for sb in await AsyncSandbox.list(labels={"run": RUN}):
      _log("sweeping leftover", sb.id, sb.state)
      await sb.delete(wait=True)
      await sb.close()

  asyncio.run(sweep())
  _log("ids", " ".join(CREATED))


async def test_environment_lifecycle_and_files():
  t0 = time.monotonic()
  env = PrizedEnvironment(**SMALL, labels=LABELS)
  await env.initialize()
  CREATED.append(env.sandbox_id)
  _log("created", env.sandbox_id, f"{time.monotonic() - t0:.2f}s")

  result = await env.execute("pwd && python3 --version && echo err >&2")
  assert result.exit_code == 0
  assert result.stdout.splitlines()[0] == "/home/prized/work"
  assert "Python 3" in result.stdout
  assert result.stderr == "err\n"
  assert (await env.execute("exit 3")).exit_code == 3

  await env.write_file("pkg/data.bin", bytes(range(256)))
  assert await env.read_file("pkg/data.bin") == bytes(range(256))
  assert (await env.execute("wc -c < pkg/data.bin")).stdout.strip() == "256"

  await env.write_file("/tmp/adk-live/out.txt", "outside home")
  assert await env.read_file("/tmp/adk-live/out.txt") == b"outside home"
  assert (await env.read_file("/etc/os-release")).startswith(b"PRETTY_NAME")

  with pytest.raises(FileNotFoundError):
    await env.read_file("nope.txt")
  with pytest.raises(FileNotFoundError):
    await env.read_file("/etc/nope")
  with pytest.raises(FileNotFoundError):  # what ADK's file tools report
    await env.read_file("pkg")
  with pytest.raises(PermissionError):
    await env.write_file("/root/x", "no")

  t1 = time.monotonic()
  slow = await env.execute("echo started; sleep 30", timeout=2)
  assert slow.timed_out is True
  assert slow.exit_code == -1
  assert time.monotonic() - t1 < 20

  sandbox_id = env.sandbox_id
  await env.close()
  assert await _state(sandbox_id) in ("deleting", "deleted", "gone")


async def test_pause_on_close_then_attach_keeps_files_and_processes():
  env = PrizedEnvironment(**SMALL, labels=LABELS, on_close="pause")
  await env.initialize()
  CREATED.append(env.sandbox_id)
  await env.write_file("notes.md", "remember me")
  # A process that keeps counting while we are away.
  await env.sandbox.exec(
      "nohup bash -c 'i=0; while true; do i=$((i+1)); echo $i >"
      " /home/prized/count; sleep 0.2; done' >/dev/null 2>&1 &"
  )
  await asyncio.sleep(1)
  await env.close()
  assert await _state(env.sandbox_id) in ("pausing", "paused")  # no wait

  again = PrizedEnvironment(sandbox_id=env.sandbox_id)
  await again.initialize()
  assert await again.read_file("notes.md") == b"remember me"
  first = int((await again.read_file("/home/prized/count")).decode())
  await asyncio.sleep(1)
  second = int((await again.read_file("/home/prized/count")).decode())
  assert second > first, "the counting process resumed after the pause"
  await again.close()  # attached: kept
  assert await _state(env.sandbox_id) == "running"
  sb = await AsyncSandbox.connect(env.sandbox_id)
  await sb.delete(wait=True)
  await sb.close()


async def test_fork_copies_are_independent():
  env = PrizedEnvironment(**SMALL, labels=LABELS)
  await env.initialize()
  CREATED.append(env.sandbox_id)
  await env.write_file("state.txt", "parent")
  copies = await env.fork(2)
  CREATED.extend(c.sandbox_id for c in copies)
  try:
    for i, copy in enumerate(copies):
      assert await copy.read_file("state.txt") == b"parent"
      await copy.write_file("state.txt", f"copy {i}")
    assert await env.read_file("state.txt") == b"parent"
    assert await copies[1].read_file("state.txt") == b"copy 1"
  finally:
    for copy in copies:
      await copy.close()
    await env.close()
  for sandbox_id in [env.sandbox_id] + [c.sandbox_id for c in copies]:
    assert await _state(sandbox_id) in ("deleting", "deleted", "gone")


async def test_gone_sandbox_is_replaced():
  env = PrizedEnvironment(**SMALL, labels=LABELS)
  await env.initialize()
  first = env.sandbox_id
  CREATED.append(first)
  sb = await AsyncSandbox.connect(first)
  await sb.delete(wait=True)
  await sb.close()

  result = await env.execute("echo fresh")
  assert result.stdout == "fresh\n"
  assert env.sandbox_id != first
  CREATED.append(env.sandbox_id)
  await env.close()


def test_sync_runs_on_new_event_loops():
  env = PrizedEnvironment(**SMALL, labels=LABELS)
  asyncio.run(env.initialize())
  CREATED.append(env.sandbox_id)
  assert asyncio.run(env.execute("echo one")).stdout == "one\n"
  assert asyncio.run(env.execute("echo two")).stdout == "two\n"
  sandbox_id = env.sandbox_id
  asyncio.run(env.close())
  assert asyncio.run(_state(sandbox_id)) in ("deleting", "deleted", "gone")


async def test_environment_toolset_tools_live():
  env = PrizedEnvironment(**SMALL, labels=LABELS)
  toolset = EnvironmentToolset(environment=env)
  tools = {tool.name: tool for tool in await toolset.get_tools()}
  CREATED.append(env.sandbox_id)
  ctx = mock.MagicMock()
  wrote = await tools["WriteFile"].run_async(
      args={"path": "fizz.py", "content": "print('fizz' * 2)\n"},
      tool_context=ctx,
  )
  assert wrote["status"] == "ok", wrote
  edited = await tools["EditFile"].run_async(
      args={"path": "fizz.py", "old_string": "* 2", "new_string": "* 3"},
      tool_context=ctx,
  )
  assert edited["status"] == "ok", edited
  read = await tools["ReadFile"].run_async(
      args={"path": "fizz.py"}, tool_context=ctx
  )
  assert "* 3" in str(read)
  ran = await tools["Execute"].run_async(
      args={"command": "python3 fizz.py"}, tool_context=ctx
  )
  assert ran == {"status": "ok", "stdout": "fizzfizzfizz\n"}
  await toolset.close()
  assert await _state(env.sandbox_id) in ("deleting", "deleted", "gone")


class ScriptedLlm(BaseLlm):
  """Plays back a fixed list of model turns (no LLM, no API key)."""

  model: str = "scripted"
  turns: list[types.Content] = []
  seen: list[LlmRequest] = []

  async def generate_content_async(
      self, llm_request: LlmRequest, stream: bool = False
  ) -> AsyncGenerator[LlmResponse, None]:
    self.seen.append(llm_request)
    turn = self.turns[len(self.seen) - 1]
    yield LlmResponse(content=turn)


def _call(name: str, **args) -> types.Content:
  return types.Content(
      role="model",
      parts=[
          types.Part(function_call=types.FunctionCall(name=name, args=args))
      ],
  )


def _text(text: str) -> types.Content:
  return types.Content(role="model", parts=[types.Part(text=text)])


def test_agent_runs_tools_in_the_sandbox_with_sync_runner():
  """ADK's own Runner (sync run(): a new event loop per call) end to end."""
  env = PrizedEnvironment(**SMALL, labels=LABELS)
  llm = ScriptedLlm(
      turns=[
          _call(
              "WriteFile",
              path="fizzbuzz.py",
              content=(
                  "for i in range(1, 16):\n    print('FizzBuzz' if i % 15 == 0"
                  " else 'Fizz' if i % 3 == 0 else 'Buzz' if i % 5 == 0 else"
                  " i)\n"
              ),
          ),
          _call("Execute", command="python3 fizzbuzz.py | tail -1"),
          _text("Done: the last line is FizzBuzz."),
          _call("Execute", command="ls fizzbuzz.py && hostname"),
          _text("Still there."),
      ]
  )
  agent = LlmAgent(
      name="coder",
      model=llm,
      instruction="Write and run code in the sandbox.",
      tools=[EnvironmentToolset(environment=env)],
  )
  runner = InMemoryRunner(agent=agent, app_name="prized-adk-live")
  session = asyncio.run(
      runner.session_service.create_session(
          app_name="prized-adk-live", user_id="u"
      )
  )

  def ask(text: str) -> list:
    return list(
        runner.run(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=text)]
            ),
        )
    )

  events = ask("Write fizzbuzz and run it.")
  CREATED.append(env.sandbox_id)
  responses = [
      p.function_response.response
      for e in events
      if e.content
      for p in e.content.parts
      if p.function_response
  ]
  assert responses[0]["status"] == "ok", responses
  assert responses[1] == {"status": "ok", "stdout": "FizzBuzz\n"}, responses
  # The environment told the model where it works.
  assert "/home/prized/work" in str(llm.seen[0].config.system_instruction)

  # A second turn: a new event loop, the same sandbox.
  events = ask("Is the file still there?")
  responses = [
      p.function_response.response
      for e in events
      if e.content
      for p in e.content.parts
      if p.function_response
  ]
  assert responses[0]["status"] == "ok", responses
  assert "fizzbuzz.py" in responses[0]["stdout"]

  asyncio.run(runner.close())
  assert asyncio.run(_state(env.sandbox_id)) in ("deleting", "deleted", "gone")


def test_code_executor_fresh_sandbox_with_files():
  executor = PrizedCodeExecutor(**SMALL, timeout_seconds=60)
  csv = File(
      name="data_1_1.csv",
      content=base64.b64encode(b"x,y\n1,2\n3,4\n").decode(),
      mime_type="text/csv",
  )
  code = (
      "import csv\n"
      "rows = list(csv.DictReader(open('data_1_1.csv')))\n"
      "total = sum(int(r['y']) for r in rows)\n"
      "open('total.txt', 'w').write(str(total))\n"
      "print('total', total)\n"
  )
  before = {s.id for s in prized.Sandbox.list(labels={"adk": "code-executor"})}
  result = executor.execute_code(
      mock.MagicMock(), CodeExecutionInput(code=code, input_files=[csv])
  )
  assert result.stdout == "total 6\n", result
  assert result.stderr == ""
  assert [f.name for f in result.output_files] == ["total.txt"]
  assert base64.b64decode(result.output_files[0].content) == b"6"
  after = {
      s.id
      for s in prized.Sandbox.list(labels={"adk": "code-executor"})
      if s.state not in ("deleting", "deleted")
  }
  assert after <= before, "the per-execution sandbox was deleted"


def test_code_executor_kept_sandbox_and_timeout():
  executor = PrizedCodeExecutor(**SMALL, keep_sandbox=True, timeout_seconds=3)
  try:
    first = executor.execute_code(
        mock.MagicMock(),
        CodeExecutionInput(code="open('kept.txt','w').write('1'); print('a')"),
    )
    CREATED.append(executor._sandbox.id)
    assert first.stdout == "a\n"
    second = executor.execute_code(
        mock.MagicMock(),
        CodeExecutionInput(code="print(open('kept.txt').read())"),
    )
    assert second.stdout == "1\n"
    slow = executor.execute_code(
        mock.MagicMock(),
        CodeExecutionInput(
            code="import time\nprint('tick', flush=True)\ntime.sleep(30)"
        ),
    )
    assert "timed out after 3 seconds" in slow.stderr
    sandbox_id = executor._sandbox.id
  finally:
    executor.close()
  assert asyncio.run(_state(sandbox_id)) in ("deleting", "deleted", "gone")


def test_code_executor_in_an_agent():
  """LlmAgent(code_executor=...) runs the model's code block in a sandbox."""
  llm = ScriptedLlm(
      turns=[
          _text("```python\nprint(sum(range(101)))\n```"),
          _text("The sum is 5050."),
      ]
  )
  agent = LlmAgent(
      name="calc",
      model=llm,
      instruction="Use Python.",
      code_executor=PrizedCodeExecutor(**SMALL),
  )
  runner = InMemoryRunner(agent=agent, app_name="prized-adk-live")
  session = asyncio.run(
      runner.session_service.create_session(
          app_name="prized-adk-live", user_id="u"
      )
  )
  events = list(
      runner.run(
          user_id="u",
          session_id=session.id,
          new_message=types.Content(
              role="user", parts=[types.Part(text="Sum 0..100")]
          ),
      )
  )
  results = [
      p.code_execution_result
      for e in events
      if e.content
      for p in e.content.parts
      if p.code_execution_result
  ]
  assert results and "5050" in results[0].output, events
  asyncio.run(runner.close())


def test_gone_sandbox_is_replaced_between_sync_calls():
  """Sync Runner.run: a new event loop per call, and the sandbox gone between."""
  env = PrizedEnvironment(**SMALL, labels=LABELS)
  asyncio.run(env.initialize())
  first = env.sandbox_id
  CREATED.append(first)

  async def delete_behind_its_back():
    sb = await AsyncSandbox.connect(first)
    await sb.delete(wait=True)
    await sb.close()

  asyncio.run(delete_behind_its_back())
  result = asyncio.run(env.execute("echo 2"))
  CREATED.append(env.sandbox_id)
  assert result.stdout == "2\n"
  assert "fresh, empty sandbox" in result.stderr
  assert env.sandbox_id != first
  asyncio.run(env.close())
  assert asyncio.run(_state(env.sandbox_id)) in ("deleting", "deleted", "gone")


async def test_symlinks_modes_and_pip():
  env = PrizedEnvironment(**SMALL, labels=LABELS)
  await env.initialize()
  CREATED.append(env.sandbox_id)
  try:
    # A symlinked file reads and writes through to its target.
    await env.execute("echo rules > AGENTS.md && ln -s AGENTS.md CLAUDE.md")
    assert await env.read_file("CLAUDE.md") == b"rules\n"
    await env.write_file("CLAUDE.md", "new rules\n")
    check = await env.execute("cat AGENTS.md; test -L CLAUDE.md && echo link")
    assert check.stdout == "new rules\nlink\n"
    # Editing a script keeps it executable.
    await env.execute("printf 'echo v1\\n' > run.sh && chmod 755 run.sh")
    await env.write_file("run.sh", "echo v2\n")
    assert (await env.execute("./run.sh")).stdout == "v2\n"
    # pip install works without root, and its commands are on PATH.
    installed = await env.execute(
        "pip install -q pyfiglet && pyfiglet -f digital ok", timeout=180
    )
    assert installed.exit_code == 0, installed.stderr
    assert "|o|k|" in installed.stdout
  finally:
    await env.close()


async def test_reinitialize_after_pause_continues_in_the_same_sandbox():
  env = PrizedEnvironment(**SMALL, labels=LABELS, on_close="pause")
  await env.initialize()
  CREATED.append(env.sandbox_id)
  await env.write_file("state.txt", "kept")
  await env.close()
  await env.initialize()  # an `adk web` reload
  try:
    assert env.sandbox_id == CREATED[-1]
    assert await env.read_file("state.txt") == b"kept"
  finally:
    sb = await AsyncSandbox.connect(env.sandbox_id)
    await sb.delete(wait=True)
    await sb.close()
