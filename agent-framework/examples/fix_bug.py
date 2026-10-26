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

"""A Microsoft Agent Framework agent fixes a seeded bug inside a Prized sandbox.

Needs PRIZED_API_KEY, plus a model: by default OpenRouter (OPENROUTER_API_KEY) through the
framework's OpenAI-compatible client (`pip install agent-framework-openai`). Pass --model to
pick one. The sandbox is deleted at the end; the model's key never enters it.
"""

import argparse
import asyncio
import os
import time

from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework_tools.shell import ShellEnvironmentProvider

from agent_framework_prized import PrizedShellTool

CALC = """def mean(values):
    # The arithmetic mean of a non-empty list.
    return sum(values) / (len(values) - 1)


def clamp(x, low, high):
    return max(low, min(x, high))
"""

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

RUN_TESTS = "python3 -m unittest -q 2>&1 | tail -1"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="anthropic/claude-haiku-5.5")
    args = parser.parse_args()

    client = OpenAIChatCompletionClient(
        model=args.model, api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1"
    )
    started = time.monotonic()
    # The sandbox is the boundary, so the model's commands run without asking.
    async with PrizedShellTool(approval_mode="never_require", labels={"example": "maf-fix-bug"}) as shell:
        for name, text in (("calc.py", CALC), ("test_calc.py", TESTS)):
            await shell.sandbox.files.write(f"{shell.working_dir}/{name}", text)
        before = await shell.run(RUN_TESTS)
        print(f"sandbox {shell.sandbox_id} ready in {time.monotonic() - started:.1f}s; before: {before.stdout.strip()}")

        agent = Agent(
            client=client,
            instructions="You are a careful engineer. Work only in the sandbox with your shell tool.",
            tools=[shell.as_function()],
            context_providers=[ShellEnvironmentProvider(shell, shell.environment_options())],
        )
        response = await agent.run(
            "The project in the working directory has failing unit tests (python3 -m unittest). "
            "Find the bug in calc.py, fix it without changing the tests, and rerun the tests until they pass."
        )
        print("agent:", response.text.strip()[:400])
        after = await shell.run(RUN_TESTS)
        print(f"after: {after.stdout.strip()}")
        calls = [c for m in response.messages for c in m.contents if c.type == "function_call"]
        usage = response.usage_details or {}
        tokens = f"{usage.get('input_token_count')}/{usage.get('output_token_count')}"
        print(f"tool calls: {len(calls)}; tokens in/out: {tokens}")
        for call in calls:
            command = str((call.parse_arguments() or {}).get("command", ""))
            print("  $", command[:160].replace("\n", "\\n"))
        print(f"total {time.monotonic() - started:.1f}s; sandbox {shell.sandbox_id} is deleted on exit")
        if after.stdout.strip() != "OK":
            raise SystemExit("tests still fail")


if __name__ == "__main__":
    asyncio.run(main())
