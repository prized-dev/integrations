"""An Inspect task whose agent fixes a seeded bug inside a Prized sandbox.

    pip install inspect-prized openai
    export PRIZED_API_KEY=dcp_...
    inspect eval examples/fix_bug.py --model openrouter/anthropic/claude-haiku-5.5

Each sample gets its own sandbox; Inspect copies the sample's files in, the agent works with
the bash tool, the scorer runs the tests in the same sandbox, and cleanup deletes it.
"""

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer
from inspect_ai.solver import TaskState, basic_agent, system_message
from inspect_ai.tool import bash
from inspect_ai.util import sandbox

from inspect_prized import PrizedSandboxConfig

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


@scorer(metrics=[accuracy()])
def tests_pass():
    async def score(state: TaskState, target: Target) -> Score:
        result = await sandbox().exec(["python3", "-m", "unittest", "-q"])
        tests_now = await sandbox().read_file("test_calc.py")
        ok = result.success and tests_now == TESTS
        return Score(value=CORRECT if ok else INCORRECT, explanation=(result.stderr or result.stdout)[-400:])

    return score


@task
def fix_bug() -> Task:
    return Task(
        dataset=[
            Sample(
                input="The project in the current directory has failing unit tests (python3 -m unittest). "
                "Find the bug in calc.py and fix it without changing the tests.",
                files={"calc.py": CALC, "test_calc.py": TESTS},
            )
        ],
        solver=basic_agent(init=system_message("You are a careful engineer."), tools=[bash(timeout=120)]),
        scorer=tests_pass(),
        sandbox=("prized", PrizedSandboxConfig(vcpu=1, memory_gb=2, labels={"example": "inspect-fix-bug"})),
    )
