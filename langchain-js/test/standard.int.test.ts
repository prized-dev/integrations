// LangChain's standard sandbox tests, run against real Prized sandboxes.
// Needs PRIZED_API_KEY; run with `npm run test:int`.
import { sandboxStandardTests } from "@langchain/sandbox-standard-tests/vitest";
import { PrizedSandbox } from "../src/index.js";

const labels: Record<string, string> = { test: "prized-langchain" };
if (process.env.PRIZED_TEST_TASK) labels.task = process.env.PRIZED_TEST_TASK;

sandboxStandardTests({
  name: "PrizedSandbox",
  skip: !process.env.PRIZED_API_KEY,
  timeout: 180_000,
  createSandbox: (options) =>
    PrizedSandbox.create({ resources: { vcpu: 1, memoryGb: 2 }, labels, maxRuntimeMin: 30, initialFiles: options?.initialFiles }),
  createUninitializedSandbox: () => new PrizedSandbox({ resources: { vcpu: 1, memoryGb: 2 }, labels, maxRuntimeMin: 30 }),
  closeSandbox: (sandbox) => sandbox.close(),
  resolvePath: (relativePath) => `/home/prized/work/${relativePath}`,
});
