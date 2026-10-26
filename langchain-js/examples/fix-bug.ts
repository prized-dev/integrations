// A Deep Agent fixes a seeded bug inside a Prized sandbox.
//
// Needs PRIZED_API_KEY and a model. By default it uses OpenRouter (OPENROUTER_API_KEY) through
// @langchain/openai; set MODEL to pick another. The model's key stays on this machine, and the
// sandbox is deleted at the end.
//
//   npx tsx examples/fix-bug.ts
import { ChatOpenAI } from "@langchain/openai";
import { createDeepAgent } from "deepagents";
import { PrizedSandbox } from "../src/index.js";

const CALC = `def mean(values):
    """The arithmetic mean of a non-empty list."""
    return sum(values) / (len(values) - 1)


def clamp(x, low, high):
    return max(low, min(x, high))
`;

const TESTS = `import unittest

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
`;

const started = Date.now();
const labels: Record<string, string> = { example: "langchain-js-fix-bug" };
if (process.env.PRIZED_TEST_TASK) labels.task = process.env.PRIZED_TEST_TASK;

const sandbox = await PrizedSandbox.create({ labels, initialFiles: { "calc.py": CALC, "test_calc.py": TESTS } });
try {
  const before = await sandbox.execute("python3 -m unittest -q 2>&1 | tail -1");
  console.log(`sandbox ${sandbox.id} ready in ${((Date.now() - started) / 1000).toFixed(1)}s; before: ${before.output.trim()}`);

  const model = new ChatOpenAI({
    model: process.env.MODEL ?? "anthropic/claude-haiku-5.5",
    apiKey: process.env.OPENROUTER_API_KEY,
    configuration: { baseURL: "https://openrouter.ai/api/v1" },
  });
  const agent = createDeepAgent({
    model,
    systemPrompt: "You are a careful engineer. The project is in /home/prized/work. Use your tools.",
    backend: sandbox,
  });
  const result = await agent.invoke({
    messages: [
      {
        role: "user",
        content:
          "The project in /home/prized/work has failing unit tests (python3 -m unittest). " +
          "Find the bug in calc.py, fix it without changing the tests, and rerun the tests until they pass.",
      },
    ],
  });
  const tools = result.messages.filter((m: { getType?: () => string }) => m.getType?.() === "tool").length;
  const last = result.messages[result.messages.length - 1];
  console.log("agent:", String(last.content).trim().slice(0, 300));

  const after = await sandbox.execute("python3 -m unittest -q 2>&1 | tail -1");
  const tests = await sandbox.downloadFiles(["/home/prized/work/test_calc.py"]);
  const unchanged = new TextDecoder().decode(tests[0].content ?? new Uint8Array()) === TESTS;
  console.log(`after: ${after.output.trim()}; tests unchanged: ${unchanged}; tool calls: ${tools}`);
  console.log(`total ${((Date.now() - started) / 1000).toFixed(1)}s`);
  if (after.output.trim() !== "OK" || !unchanged) process.exitCode = 1;
} finally {
  await sandbox.close();
}
