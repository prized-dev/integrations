# Prized integrations

Adapters that run AI agent frameworks, eval harnesses and agent platforms on [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=integrations): Firecracker microVMs that pause with their memory and fork into live copies.

Each folder is its own package with its own README, tests and release. You need a Prized API key from [prized.dev](https://prized.dev/onboarding/sandboxes?via=integrations). No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=integrations.

| Folder | Framework | Package | Status | Docs |
| --- | --- | --- | --- | --- |
| [`openai-agents`](openai-agents) | OpenAI Agents SDK: sandbox client | `openai-agents-prized` | Works now: on PyPI | [README](openai-agents/README.md) |
| [`hermes`](hermes) | Hermes Agent: terminal backend plugin | `hermes plugins install prized-dev/integrations/hermes` | Works now | [README](hermes/README.md) |
| [`harbor`](harbor) | Harbor (Terminal-Bench): environment | `harbor-prized` | Works now: install from this repo (not on PyPI yet) | [README](harbor/README.md) |
| [`agentenv`](agentenv) | AgentEnv Framework (Scale's `agent-env`): sandbox provider | `agentenv-prized` | Early: sandboxes, commands and files work; gateway and agent deploys wait on Prized's Docker template and port URLs | [README](agentenv/README.md) |
| [`dify`](dify) | Dify: tool plugin (workflows and agents) | `prized_sandboxes` (`.difypkg`) | Build the `.difypkg` from this repo; tested under Dify's plugin runtime, not yet in a Dify install (self-hosted Dify takes the unsigned package only with signature checks off); Marketplace submission pending | [README](dify/README.md) |
| [`devin-outposts`](devin-outposts) | Devin Outposts: orchestrator (one sandbox per session, paused with memory while it sleeps) | `prized-devin-outposts` | Works now: install from this repo (not on PyPI yet); Devin's one-click connect waits on Cognition | [README](devin-outposts/README.md) |
| [`claude-managed-agents`](claude-managed-agents) | Claude Managed Agents: self-hosted environment | `on-work.sh` | Works now | [README](claude-managed-agents/README.md) |
| [`cursor`](cursor) | Cursor Cloud Agents: Team Pool spawn hook (one sandbox per agent) | `spawn.sh` | Team Pools need Cursor Enterprise; not yet run in a real pool, tested end to end with a My Machines worker | [README](cursor/README.md) |
| [`inspect`](inspect) | Inspect AI: sandbox environment | `inspect-prized` | Coming 2026-11-05 | [README](inspect/README.md) |
| [`adk`](adk) | Google ADK: environment and code executor | `prized-adk` | Coming 2026-11-05 | [README](adk/README.md) |
| [`strands`](strands) | Strands Agents: sandbox | `strands-prized` | Coming 2026-11-05 | [README](strands/README.md) |
| [`agent-framework`](agent-framework) | Microsoft Agent Framework: shell tool and file store | `agent-framework-prized` | Coming 2026-11-05 | [README](agent-framework/README.md) |
| [`pydantic-ai`](pydantic-ai) | Pydantic AI: workspace | `pydantic-ai-prized` | Coming 2026-11-05 | [README](pydantic-ai/README.md) |
| [`langchain-js`](langchain-js) | LangChain Deep Agents (JS): sandbox | `@prized/langchain` | Coming 2026-11-05 | [README](langchain-js/README.md) |
| [`mastra`](mastra) | Mastra: workspace sandbox and filesystem | `@prized/mastra` | Coming 2026-11-05 | [README](mastra/README.md) |

"Coming 2026-11-05" means the package itself isn't on PyPI or npm yet. The Prized SDKs it uses already are (`prized` on PyPI, `@prized/sdk` on npm).

Also from Prized, outside this repo: the SDKs themselves (`prized` on PyPI, `@prized/sdk` on npm), `langchain-prized` (LangChain Python, on PyPI), and the Claude Code and Codex plugin in [prized-dev/skills](https://github.com/prized-dev/skills).

## Tests

Every folder's tests run offline, with fakes in place of the Prized API:

```sh
scripts/test.sh inspect          # lint + offline tests in a fresh virtualenv
FLOOR=1 scripts/test.sh inspect  # against the oldest supported framework versions
```

CI runs the same script for every folder on each push and pull request, with no secrets. Live tests against the real API are opt-in per folder (see its README).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report a security issue to team@prized.dev, not in a public issue.

## License

[Apache-2.0](LICENSE)
