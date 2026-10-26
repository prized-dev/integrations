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

"""An ADK agent fixes a seeded bug inside a Prized sandbox.

Needs PRIZED_API_KEY and a model. By default Gemini through OpenRouter
(OPENROUTER_API_KEY, `pip install litellm`); pass --model gemini-flash-latest
to use GOOGLE_API_KEY directly. The model's key never enters the sandbox.
"""

import argparse
import asyncio
import time

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools.environment import EnvironmentToolset
from google.genai import types

from prized_adk import PrizedEnvironment

CALC = '''def mean(values):
    """The arithmetic mean of a non-empty list."""
    return sum(values) / (len(values) - 1)


def clamp(x, low, high):
    return max(low, min(x, high))
'''

TESTS = """import unittest

from calc import clamp, mean


class CalcTest(unittest.TestCase):
  def test_mean(self):
    self.assertEqual(mean([2, 4, 6]), 4)

  def test_mean_single(self):
    self.assertEqual(mean([5]), 5)

  def test_clamp(self):
    self.assertEqual(clamp(15, 0, 10), 10)


if __name__ == "__main__":
  unittest.main()
"""


async def main(model_name: str) -> None:
  if model_name.startswith("openrouter/"):
    from google.adk.models.lite_llm import LiteLlm

    model = LiteLlm(model=model_name)
  else:
    model = model_name

  env = PrizedEnvironment(labels={"example": "adk-fix-bug"})
  agent = LlmAgent(
      name="fixer",
      model=model,
      instruction="You are a careful engineer. Work only through your tools.",
      tools=[EnvironmentToolset(environment=env)],
  )
  runner = InMemoryRunner(agent=agent, app_name="fix-bug")
  started = time.monotonic()
  try:
    await env.initialize()
    await env.write_file("calc.py", CALC)
    await env.write_file("test_calc.py", TESTS)
    before = await env.execute("python3 -m unittest -q 2>&1 | tail -1")
    print(
        f"sandbox {env.sandbox_id} ready in"
        f" {time.monotonic() - started:.1f}s; before: {before.stdout.strip()}"
    )

    session = await runner.session_service.create_session(
        app_name="fix-bug", user_id="dev"
    )
    prompt = (
        "The project in the working directory has failing unit tests"
        " (python3 -m unittest). Find the bug in calc.py, fix it without"
        " changing the tests, and rerun the tests until they pass."
    )
    calls = 0
    async for event in runner.run_async(
        user_id="dev",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
      calls += len(event.get_function_calls())
      if event.is_final_response() and event.content:
        text = "".join(p.text or "" for p in event.content.parts)
        print("agent:", text.strip()[:400])

    after = await env.execute("python3 -m unittest -q 2>&1 | tail -1")
    print(f"after: {after.stdout.strip()}; tool calls: {calls}")
    print(f"total {time.monotonic() - started:.1f}s")
    if after.stdout.strip() != "OK":
      raise SystemExit("tests still fail")
  finally:
    await runner.close()  # closes the toolset, which deletes the sandbox
    await env.close()  # in case the run ended before the toolset was set up


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("--model", default="openrouter/google/gemini-3.8-flash")
  asyncio.run(main(parser.parse_args().model))
