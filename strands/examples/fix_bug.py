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

"""A Strands agent fixes a seeded bug inside a Prized sandbox.

Needs PRIZED_API_KEY, plus a model: by default OpenRouter (OPENROUTER_API_KEY) through
Strands' OpenAI provider (`pip install 'strands-agents[openai]'`). Pass --model to pick one.
The sandbox is deleted at the end; the model's key never enters it.
"""

import argparse
import os
import time

from strands import Agent
from strands.models.openai import OpenAIModel

from strands_prized import PrizedSandbox

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="anthropic/claude-haiku-5.5")
    args = parser.parse_args()

    model = OpenAIModel(
        client_args={"api_key": os.environ["OPENROUTER_API_KEY"], "base_url": "https://openrouter.ai/api/v1"},
        model_id=args.model,
    )
    with PrizedSandbox(labels={"example": "strands-fix-bug"}) as sandbox:
        started = time.monotonic()
        before = sandbox_run(sandbox, "python3 -m unittest -q 2>&1 | tail -1", seed=True)
        print(f"sandbox {sandbox.sandbox_id} ready in {time.monotonic() - started:.1f}s; before: {before.strip()}")

        agent = Agent(
            model=model,
            sandbox=sandbox,
            callback_handler=None,
            system_prompt="You are a careful engineer. Work only in the sandbox with your tools.",
        )
        reply = agent(
            "The project in /home/prized/work has failing unit tests (python3 -m unittest). "
            "Find the bug in calc.py, fix it without changing the tests, and rerun the tests until they pass."
        )
        print("agent:", str(reply).strip()[:400])
        after = sandbox_run(sandbox, "python3 -m unittest -q 2>&1 | tail -1")
        print(f"after: {after.strip()}")
        usage = agent.event_loop_metrics.accumulated_usage
        tools = sum(m.call_count for m in agent.event_loop_metrics.tool_metrics.values())
        print(f"tool calls: {tools}; tokens in/out: {usage['inputTokens']}/{usage['outputTokens']}")
        print(f"total {time.monotonic() - started:.1f}s")
        if after.strip() != "OK":
            raise SystemExit("tests still fail")


def sandbox_run(sandbox: PrizedSandbox, command: str, seed: bool = False) -> str:
    import asyncio

    async def go() -> str:
        if seed:
            await sandbox.write_text("calc.py", CALC)
            await sandbox.write_text("test_calc.py", TESTS)
        return (await sandbox.execute(command)).stdout

    return asyncio.run(go())


if __name__ == "__main__":
    main()
