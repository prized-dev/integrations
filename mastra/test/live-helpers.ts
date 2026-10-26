/** Shared by the live tests: they run only with PRIZED_API_KEY and PRIZED_LIVE=1. */
import { Sandbox } from '@prized/sdk';
import type { ResourcesInput } from '@prized/sdk';

export const LIVE: boolean = Boolean(process.env.PRIZED_API_KEY) && process.env.PRIZED_LIVE === '1';

/** Every sandbox a test run creates carries `run=<RUN>`, so the run can delete what it left. */
export const RUN: string = `mastra-test-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;

/** `created-by`, `run`, and any `k=v,k=v` pairs from PRIZED_TEST_LABELS. */
export function testLabels(): Record<string, string> {
  const extra = Object.fromEntries(
    (process.env.PRIZED_TEST_LABELS ?? '')
      .split(',')
      .map(pair => pair.trim())
      .filter(pair => pair.includes('='))
      .map(pair => [pair.slice(0, pair.indexOf('=')), pair.slice(pair.indexOf('=') + 1)]),
  );
  return { ...extra, 'created-by': 'prized-mastra-test', run: RUN };
}

/** A small shape that fits the free tier: PRIZED_TEST_VCPU vCPU (default 2) and 4 GB. */
export function testResources(): ResourcesInput {
  return { vcpu: Number(process.env.PRIZED_TEST_VCPU ?? 2), memoryGb: 4 };
}

/** Delete every sandbox this run created and wait until none is listed. Returns the ids deleted. */
export async function sweep(): Promise<string[]> {
  const deleted = await Sandbox.deleteByLabels({ run: RUN });
  for (let i = 0; i < 60; i++) {
    const left = await Sandbox.list({ labels: { run: RUN } });
    if (left.length === 0) break;
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  return deleted;
}
