/**
 * A Mastra agent fixes a failing test in a Prized sandbox.
 *
 *   PRIZED_API_KEY=dcp_... OPENAI_API_KEY=sk-... npm run example
 *
 * MODEL picks another model from Mastra's model router (default openai/gpt-5-nano). The run stops
 * once the model's tokens would cost more than MAX_USD (default 0.10, priced for gpt-5-nano). The
 * sandbox (2 vCPU, 4 GB, labelled example=prized-mastra plus EXAMPLE_LABELS) is deleted at the end,
 * failures included.
 */
import { createHash } from 'node:crypto';

import { Agent } from '@mastra/core/agent';
import { Workspace } from '@mastra/core/workspace';

import { PrizedSandbox } from '../src/index.js';

const MODEL = process.env.MODEL ?? 'openai/gpt-5-nano';
const MAX_USD = Number(process.env.MAX_USD ?? 0.1);
// gpt-5-nano, US$ per million tokens (developers.openai.com/api/docs/pricing, 2026-10-26).
const PRICE = { input: 0.05, cachedInput: 0.005, output: 0.4 };

const APP = '/home/prized/work/app';
const FILES = {
  'app/stats.py': `def mean(values):
    """The arithmetic mean of a non-empty list of numbers."""
    total = 0
    for v in values[1:]:
        total += v
    return total / len(values)
`,
  'app/test_stats.py': `import unittest

from stats import mean


class MeanTest(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([2, 4, 6]), 4)

    def test_single(self):
        self.assertEqual(mean([5]), 5)


if __name__ == "__main__":
    unittest.main()
`,
};

const extraLabels = Object.fromEntries(
  (process.env.EXAMPLE_LABELS ?? '')
    .split(',')
    .filter(pair => pair.includes('='))
    .map(pair => [pair.slice(0, pair.indexOf('=')), pair.slice(pair.indexOf('=') + 1)]),
);

const sandbox = new PrizedSandbox({
  resources: { vcpu: 2, memoryGb: 4 },
  labels: { ...extraLabels, example: 'prized-mastra' },
  maxRuntimeMin: 30,
  autoDeleteMin: 60, // if this script dies before it deletes the sandbox, Prized does after it pauses
});

const workspace = new Workspace({ sandbox });
const agent = new Agent({
  id: 'fixer',
  name: 'Fixer',
  instructions:
    'You fix bugs. Use execute_command to look around, run the tests, and edit files (with python3, sed or a heredoc). ' +
    'Fix the code, not the tests. Stop once the tests pass and say what you changed.',
  model: MODEL,
  workspace,
});

/** US$ for one step's tokens. Cached input is read from either usage shape the AI SDK reports. */
function costOf(usage: unknown): number {
  const u = (usage ?? {}) as {
    inputTokens?: number;
    outputTokens?: number;
    cachedInputTokens?: number;
    inputTokenDetails?: { cacheReadTokens?: number };
  };
  const cached = u.cachedInputTokens ?? u.inputTokenDetails?.cacheReadTokens ?? 0;
  return (((u.inputTokens ?? 0) - cached) * PRICE.input + cached * PRICE.cachedInput + (u.outputTokens ?? 0) * PRICE.output) / 1e6;
}

const sha = (s: string): string => createHash('sha256').update(s).digest('hex');
const t0 = Date.now();
const seconds = (): string => ((Date.now() - t0) / 1000).toFixed(1);

let usd = 0;
let exitCode = 1;
const budget = new AbortController();
try {
  await sandbox.start();
  console.log(`[${seconds()}s] sandbox ${sandbox.sandboxId} running`);
  await sandbox.writeFiles(Object.entries(FILES).map(([path, content]) => ({ path: `/home/prized/work/${path}`, content })));
  const before = await sandbox.executeCommand!('python3', ['-m', 'unittest', '-q'], { cwd: APP });
  console.log(`[${seconds()}s] tests before: exit ${before.exitCode}`);

  let calls = 0;
  const result = await agent.generate(`The tests in ${APP} fail. Make them pass.`, {
    maxSteps: 15,
    abortSignal: budget.signal,
    onStepFinish: step => {
      usd += costOf(step.usage);
      calls += step.toolCalls?.length ?? 0;
      if (usd > MAX_USD) budget.abort(new Error(`over the US$${MAX_USD} budget`));
    },
  });
  console.log(`[${seconds()}s] agent finished: ${calls} tool calls, about US$${usd.toFixed(4)}`);
  console.log(`agent: ${result.text.trim()}`);

  // Check the work ourselves: the tests pass and the test file is unchanged.
  const after = await sandbox.executeCommand!('python3', ['-m', 'unittest', '-q'], { cwd: APP });
  const testFile = await sandbox.executeCommand!('cat', [`${APP}/test_stats.py`]);
  const testsUntouched = sha(testFile.stdout) === sha(FILES['app/test_stats.py']);
  console.log(`[${seconds()}s] tests after: exit ${after.exitCode}; test file unchanged: ${testsUntouched}`);
  exitCode = after.exitCode === 0 && testsUntouched ? 0 : 1;
} finally {
  await workspace.destroy(); // deletes the sandbox
  console.log(`[${seconds()}s] sandbox deleted; ${exitCode === 0 ? 'FIXED' : 'NOT FIXED'}`);
}
process.exitCode = exitCode;
