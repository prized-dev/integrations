# @prized/langchain

> **Coming 2026-11-05.** `@prized/langchain` and the Prized TypeScript SDK it uses (`@prized/sdk`) are not on npm yet.

A [Deep Agents](https://github.com/langchain-ai/deepagentsjs) (LangChain JS) sandbox backend on [Prized](https://prized.dev/docs/sandboxes): the agent's `execute` tool and its file tools (`read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep`) run in a Prized sandbox, a Firecracker microVM, instead of on your machine.

For Python, see `langchain-prized` (on PyPI from 2026-11-05).

## Install

```sh
npm install @prized/langchain deepagents @langchain/anthropic
export PRIZED_API_KEY=dcp_...   # prized.dev/dashboard/workspace/api-keys
```

## Use

```ts
import { createDeepAgent } from "deepagents";
import { ChatAnthropic } from "@langchain/anthropic";
import { PrizedSandbox } from "@prized/langchain";

const sandbox = await PrizedSandbox.create();
try {
  const agent = createDeepAgent({
    model: new ChatAnthropic({ model: "claude-sonnet-5-5" }),
    systemPrompt: "You are a coding assistant with a sandbox. Work in /home/prized/work.",
    backend: sandbox,
  });
  await agent.invoke({ messages: [{ role: "user", content: "Write a fizzbuzz in Python and run it" }] });
} finally {
  await sandbox.close(); // deletes the sandbox
}
```

`examples/fix-bug.ts` is a full run: an agent fixes a failing test in a sandbox with any OpenAI-compatible model.

Keep a sandbox between sessions: create it with `PrizedSandbox.create()`, keep `sandbox.id`, and later `await PrizedSandbox.fromId(id)`. An idle sandbox pauses itself after 10 minutes (files, memory and processes kept, no compute charged) and the next command resumes it. `close()` on an attached sandbox leaves it running; `kill()` deletes it.

## Options

| Option | Default | Meaning |
|---|---|---|
| `sandboxId` | none | Attach to an existing sandbox. |
| `template` | `prized-base-v1` | `prized-base-v1` (Ubuntu 24.04, Python, Node, git) or `prized-agent-v1` (adds Claude Code and Codex). |
| `resources` | template's | `{ vcpu, memoryGb, diskGb }` for a new sandbox. |
| `envVars`, `labels` | none | Variables for every command; labels to find the sandbox later (`created-by=prized-langchain` is always added). |
| `autoPauseMin` | `10` | Idle minutes before a new sandbox pauses itself; `0` turns it off. |
| `maxRuntimeMin` | 24 hours (60 minutes on the free tier) | Running minutes before Prized deletes a new sandbox. |
| `timeout` | `240` | Seconds a command may run; past it the command and its children are killed and the result has exit code 124. |
| `cwd` | `/home/prized/work` | Where commands start. |
| `initialFiles` | none | Text files written once the sandbox is ready. |
| `apiKey`, `baseUrl` | `PRIZED_API_KEY` | Connection. |

Commands run under bash as the user `prized` (no sudo). `pip install` works and `~/.local/bin` is on the PATH. Each output stream keeps its first 4 MiB. File paths are absolute; uploads replace a file whole, keep an existing file's mode, and write through symlinks.

## Tests

```sh
npm test                 # offline unit tests
npm run test:int         # LangChain's standard sandbox tests against the real API (needs PRIZED_API_KEY)
```

## License

Apache-2.0
