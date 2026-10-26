# Prized integrations

Adapters that run AI agent frameworks, eval harnesses and agent platforms on [Prized Sandboxes](https://prized.dev/docs/sandboxes): Firecracker microVMs that pause with their memory and fork into live copies.

Each folder is its own package with its own README, tests and release. You need a Prized API key from [prized.dev](https://prized.dev/onboarding/sandboxes).

| Folder | Framework | Package | Status | Docs |
| --- | --- | --- | --- | --- |
| [`hermes`](hermes) | Hermes Agent: terminal backend plugin | `hermes plugins install prized-dev/integrations/hermes` | Works now | [README](hermes/README.md) |
| [`harbor`](harbor) | Harbor (Terminal-Bench): environment | `harbor-prized` | Works now from this repo; on PyPI soon | [README](harbor/README.md) |
| [`claude-managed-agents`](claude-managed-agents) | Claude Managed Agents: self-hosted environment | `on-work.sh` | Works now | [README](claude-managed-agents/README.md) |
| [`inspect`](inspect) | Inspect AI: sandbox environment | `inspect-prized` | Waits on SDK publish (2026-11-05) | [README](inspect/README.md) |
| [`adk`](adk) | Google ADK: environment and code executor | `prized-adk` | Waits on SDK publish (2026-11-05) | [README](adk/README.md) |
| [`strands`](strands) | Strands Agents: sandbox | `strands-prized` | Waits on SDK publish (2026-11-05) | [README](strands/README.md) |
| [`agent-framework`](agent-framework) | Microsoft Agent Framework: shell tool and file store | `agent-framework-prized` | Waits on SDK publish (2026-11-05) | [README](agent-framework/README.md) |
| [`pydantic-ai`](pydantic-ai) | Pydantic AI: workspace | `pydantic-ai-prized` | Waits on SDK publish (2026-11-05) | [README](pydantic-ai/README.md) |
| [`langchain-js`](langchain-js) | LangChain Deep Agents (JS): sandbox | `@prized/langchain` | Waits on SDK publish (2026-11-05) | [README](langchain-js/README.md) |

"Waits on SDK publish" means the package needs the Prized Python SDK (`prized`) or TypeScript SDK (`@prized/sdk`), which go on PyPI and npm on 2026-11-05. The packages follow right after.

Also from Prized, outside this repo: the SDKs themselves, `langchain-prized` (LangChain Python) and `openai-agents-prized` (OpenAI Agents SDK), all on PyPI and npm from 2026-11-05, and the Claude Code and Codex plugin in [prized-dev/skills](https://github.com/prized-dev/skills).

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
