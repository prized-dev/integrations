/**
 * Run Claude Code on the git repo you're in, in a Prized sandbox, and bring its commits back:
 *
 *   PRIZED_API_KEY=dcp_... npx tsx examples/run.ts "Add a CONTRIBUTING.md"
 *
 * Declare the agent's key in .sandcastle/.env (`CLAUDE_CODE_OAUTH_TOKEN=` or `ANTHROPIC_API_KEY=`),
 * with its value in your environment.
 */
import { run, claudeCode } from '@ai-hero/sandcastle';
import { prized } from '../src/index.js';

const prompt = process.argv[2] ?? 'Add a one-line summary of this repo to NOTES.md and commit it.';

const result = await run({
  agent: claudeCode('claude-opus-4-8'),
  sandbox: prized({ resources: { vcpu: 2, memoryGb: 4 } }),
  prompt,
  maxIterations: 1,
});
console.log(`${result.commits.length} commit(s) on ${result.branch}`);
