# Prized Sandbox for Paseo

A [Paseo](https://paseo.sh) plugin that runs Paseo hosts on [Prized sandboxes](https://prized.dev/docs/sandboxes?via=paseo). Each host is a sandbox with its own Paseo daemon, Claude Code and Codex. **Stop** pauses it with its memory: the daemon, its terminals and the agents' processes are kept, and only storage is billed. **Resume** carries on from the same point, with no new pairing.

For a Paseo daemon on a Prized box instead (a dev machine of your own, over SSH), see [Run Paseo's agents on a Prized box](https://prized.dev/docs/paseo?via=paseo).

## Install

Paseo 0.14 or later on the daemon and the app, with **Settings → Plugins → Enable plugins** on. Then:

```bash
paseo plugin add github:prized-dev/integrations:paseo
```

or paste `github:prized-dev/integrations:paseo` into **Settings → Plugins → Plugin source**. Nothing is installed or built on the daemon's machine: Paseo compiles the plugin, which has no runtime dependencies.

## Set up

1. Make a Prized API key with **Sandboxes: Write** at [prized.dev](https://prized.dev/dashboard/workspace/api-keys?via=paseo).
2. Open **Settings → Plugins → Prized Sandbox** and save it, or set `PRIZED_API_KEY` in the environment the Paseo daemon starts from.
3. Optional: save an Anthropic and an OpenAI API key there too. Sandboxes created afterwards get them as `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`, so Claude Code and Codex start signed in.

## Use

1. Open **Prized sandboxes** in the sidebar and press **Create host**. About 40 seconds later it reads **Running**.
2. **Copy pairing link**, then paste it into **Hosts → Add host**. Or press **Add to this daemon's hosts**.
3. Create a project on the new host under `/home/prized/work` (clone your repository there), and start agents as on any host.
4. **Stop** when you are done for now; **Resume** later. **Delete** when you no longer need it (it asks for the sandbox ID).

## What happens

- **Create** makes a sandbox from `prized-agent-v1` (Ubuntu 24.04, Node.js 22, Claude Code, Codex; 2 vCPU and 4 GB unless you choose another size), labelled `via=paseo`. It installs `@getpaseo/cli` 0.14.0 from npm into the sandbox's home, starts `paseo daemon`, and keeps the link `paseo daemon pair --relay` prints. The sandbox is reachable only outbound, through Paseo's relay.
- **Stop** pauses the sandbox. A paused sandbox keeps its memory, processes and disk, bills storage only (the first 20 GB of paused storage in a workspace are included), and its running-time clock stops. A model request in flight at that moment may fail when it resumes; the agent retries or you send the turn again.
- **Resume** resumes it, checks the daemon is still there (it is, after a pause) and starts it if not. The daemon reconnects to the relay by itself within a few seconds.
- **Running time.** A running sandbox is deleted when its running time ends. Unless you choose 1 hour to 7 days in the settings, Prized's default applies: 24 hours, counted again from each resume, or on free credit 60 minutes of running in all.
- **Retry.** Work cut short (an install that failed, the plugin restarted mid-create) shows the reason and a **Retry** that picks up on the same sandbox. A sandbox whose create answer was lost is found by its label (`paseo-host=<id>`), never made twice, and Delete finds it the same way.
- **Another workspace's key.** Hosts remember the workspace that made them. With a key from another workspace saved, they show that and refuse Stop, Resume and Delete instead of reading as gone.

## Data and credentials

- The Prized key and the agent keys are saved in `$PASEO_HOME/prized-sandbox/secrets.json` (mode 600), in a directory only the daemon's user can open, and are never sent back to the app.
- The Prized key goes only to `api.prized.dev` and the sandbox's command endpoint on `term.prized.dev`; it never enters a sandbox. The agent keys become the sandbox's environment variables: Prized stores them encrypted, and code in the sandbox can read them.
- Each host's pairing link opens its daemon, so it is kept with the secrets and handed to the app only when you press **Copy pairing link**.

## Limits

- Oregon (`us-west-2`) only.
- No root and no Docker inside a sandbox: the agents run as `prized`, without sudo. For Docker, use a [box](https://prized.dev/docs/paseo?via=paseo).
- Nothing reaches a sandbox from the internet: no ports to open, no SSH host.
- On Prized's free credit, a sandbox is at most 2 vCPU and 4 GB and runs 60 minutes in all (paused time does not count), then it is deleted. For agents that run for hours, add a Prized box as an SSH host instead: [Run Paseo's agents on a Prized box](https://prized.dev/docs/paseo?via=paseo).
- Sandboxes created before you change the agent keys, size or running time keep what they were created with.

## Tests

```bash
scripts/test.sh paseo
```

It typechecks against the pinned Paseo SDK and runs `node --test` against an in-memory Prized API (`test/fake-prized.mjs`), with no keys and no network. Node 22.18 or later runs the TypeScript sources directly.
