# Prized integrations

Adapters that run AI agent frameworks, eval harnesses and agent platforms on [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=integrations): Firecracker microVMs that pause with their memory and fork into live copies.

Each folder is its own package with its own README, tests and release. You need a Prized API key from [prized.dev](https://prized.dev/onboarding/sandboxes?via=integrations). No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=integrations.

| Folder | Framework | Package | Status | Docs |
| --- | --- | --- | --- | --- |
| [`openai-agents`](openai-agents) | OpenAI Agents SDK: sandbox client | `openai-agents-prized` | Works now: on PyPI | [README](openai-agents/README.md) |
| [`herdr`](herdr) | herdr: plugin (the agent in a sandbox or box, paused when its pane closes; a box as a machine) | `herdr plugin install prized-dev/integrations/herdr` | Works now: tested in herdr 0.9.3 and on live sandboxes; listed in [herdr's marketplace](https://herdr.dev/tui/plugins/) | [README](herdr/README.md) |
| [`hermes`](hermes) | Hermes Agent: terminal backend plugin | `hermes plugins install prized-dev/integrations/hermes` | Works now | [README](hermes/README.md) |
| [`harbor`](harbor) | Harbor (Terminal-Bench): environment | `harbor-prized` | Works now: install from this repo (not on PyPI yet) | [README](harbor/README.md) |
| [`paseo`](paseo) | Paseo: plugin (hosts on sandboxes; Stop pauses them with their memory and processes) | `paseo plugin add github:prized-dev/integrations:paseo` | Works now: loaded in Paseo 0.14 and run against live sandboxes; listed in [Paseo's plugin registry](https://paseo.sh/plugins/prized-dev/paseo) as `prized-dev/paseo` | [README](paseo/README.md) |
| [`dify`](dify) | Dify: tool plugin (workflows and agents) | `prized_sandboxes` (`.difypkg`) | Build the `.difypkg` from this repo; tested under Dify's plugin runtime, not yet in a Dify install (self-hosted Dify takes the unsigned package only with signature checks off); Marketplace submission pending | [README](dify/README.md) |
| [`devin-outposts`](devin-outposts) | Devin Outposts: orchestrator (one sandbox per session, paused with memory while it sleeps) | `prized-devin-outposts` | Works now: install from this repo (not on PyPI yet); Devin's one-click connect waits on Cognition | [README](devin-outposts/README.md) |
| [`github-issues`](github-issues) | GitHub issues: label an issue `prized` and the Claude Code or Codex on your Prized box opens the pull request; `/prized` comments continue the same session | `prized_issues.py` (a poller you run on your box, not a GitHub App) | Works now: run end to end with Claude Code against a stand-in for github.com; Codex and github.com itself not run yet | [README](github-issues/README.md) |
| [`claude-managed-agents`](claude-managed-agents) | Claude Managed Agents: self-hosted environment | `on-work.sh` | Works now | [README](claude-managed-agents/README.md) |
| [`gemini-managed-agents`](gemini-managed-agents) | Gemini Managed Agents: self-hosted environment (one sandbox per environment, paused with its memory between interactions) | `provision.sh` | Tested offline against stand-ins for Google's poller and the Prized CLI; not yet run against the Gemini API | [README](gemini-managed-agents/README.md) |
| [`cursor`](cursor) | Cursor Cloud Agents: Team Pool spawn hook (one sandbox per agent) | `spawn.sh` | Team Pools need Cursor Enterprise; not yet run in a real pool, tested end to end with a My Machines worker | [README](cursor/README.md) |
| [`conductor`](conductor) | Conductor: move a repo to a Prized box with a git worktree, the repo's setup script and an agent per workspace | `move.sh` (one command, from your checkout) | Works now: run end to end on a live box with public repos that use `conductor.json` and `.conductor/settings.toml` | [README](conductor/README.md) |
| [`inspect`](inspect) | Inspect AI: sandbox environment | `inspect-prized` | Not on PyPI yet: install from this repo | [README](inspect/README.md) |
| [`adk`](adk) | Google ADK: environment and code executor | `prized-adk` | Not on PyPI yet: install from this repo | [README](adk/README.md) |
| [`strands`](strands) | Strands Agents: sandbox | `strands-prized` | Not on PyPI yet: install from this repo | [README](strands/README.md) |
| [`agent-framework`](agent-framework) | Microsoft Agent Framework: shell tool and file store | `agent-framework-prized` | Not on PyPI yet: install from this repo | [README](agent-framework/README.md) |
| [`pydantic-ai`](pydantic-ai) | Pydantic AI: workspace | `pydantic-ai-prized` | Not on PyPI yet: install from this repo | [README](pydantic-ai/README.md) |
| [`langchain-js`](langchain-js) | LangChain Deep Agents (JS): sandbox | `@prized/langchain` | Not on npm yet | [README](langchain-js/README.md) |
| [`mastra`](mastra) | Mastra: workspace sandbox and filesystem | `@prized/mastra` | Not on npm yet | [README](mastra/README.md) |

A Python package that isn't on PyPI yet installs from this repo: its README gives the line. The Prized SDKs they use are published (`prized` on PyPI, `@prized/sdk` on npm).

Also from Prized, outside this repo: the SDKs themselves (`prized` on PyPI, `@prized/sdk` on npm), `langchain-prized` (LangChain Python, on PyPI), the Claude Code and Codex plugin in [prized-dev/skills](https://github.com/prized-dev/skills), and the Orca plugin in [prized-dev/orca-prized-plugin](https://github.com/prized-dev/orca-prized-plugin) (each Orca workspace in its own sandbox; listed in [Orca's plugin catalog](https://www.onorca.dev/plugins/prized-dev/orca-prized-plugin)).

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
