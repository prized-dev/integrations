# Claude Managed Agents on Prized: the webhook orchestrator

> **Not on PyPI yet.** Install it from this repo. Tested offline against a stand-in for Anthropic's API (see Tests); not yet run against a live environment.

Serve a [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) [self-hosted environment](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes) with [Prized sandboxes](https://prized.dev/docs/sandboxes?via=claude-managed-agents-orchestrator): each session gets a Firecracker microVM of its own. Claude runs on Anthropic's side; the sandbox runs the agent's tools and holds its files. A `session.status_run_started` webhook starts the work, and between runs the sandbox pauses with its memory, files and processes, so the session's next message carries on where it stopped, about a second later. When the session ends, its sandbox is deleted.

For one poller on a machine you keep running, with nothing to install but `ant` and `jq`, use [`on-work.sh`](../claude-managed-agents) instead.

## Run it

1. In the [Claude Console](https://platform.claude.com/environments), create a self-hosted environment and generate its environment key. Under [Webhooks](https://platform.claude.com/settings/workspaces/default/webhooks), add an endpoint (HTTPS on port 443, a public hostname) for `session.status_run_started`, `session.status_terminated`, `session.status_idled` and `session.deleted`, and copy its signing secret.

2. Make a Prized API key with **Sandboxes: Write** at https://prized.dev/dashboard/workspace/api-keys?via=claude-managed-agents-orchestrator. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=claude-managed-agents-orchestrator.

3. On a machine that stays on, install it and export the four values:

   ```bash
   pip install "git+https://github.com/prized-dev/integrations#subdirectory=claude-managed-agents-orchestrator"
   export ANTHROPIC_ENVIRONMENT_ID=env_…
   export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-…
   export ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_…
   export PRIZED_API_KEY=dcp_…
   ```

4. Start it, behind whatever puts your endpoint's HTTPS in front of `127.0.0.1:8787` (Caddy, nginx, a Cloudflare Tunnel; on a platform that terminates TLS for you, add `--host 0.0.0.0`):

   ```bash
   prized-managed-agents serve
   ```

   Without `ANTHROPIC_WEBHOOK_SIGNING_KEY` (or with `--poll`) it long-polls the queue instead and needs no inbound URL.

5. Start a session on the environment and send it a message. `prized sandbox ls` shows its sandbox, labelled `anthropic-session=sesn_…`; `prized-managed-agents status` shows the queue and each sandbox's runs.

## What happens

- **A webhook arrives.** Its signature is checked (Standard Webhooks, as Anthropic's SDKs do, within 5 minutes) and retries of it are dropped. A run-started event wakes the intake, which claims everything queued. The queue, not the webhook, is the record: every minute it polls once more anyway, since a lost webhook is never sent again.
- **A work item is claimed.** The session's sandbox is resumed if it has one (paused, with its files and processes). Otherwise a warm parent is forked, or a sandbox is created from `prized-base-v1`. Anthropic's worker, `ant` 2.7.1, is installed in it if missing, checked against its published sha256. The work item's secret is written into the sandbox, readable by its user only, and `ant beta:worker run` starts. Only then is the item acknowledged.
- **The secret reaches one session.** The environment key, which can claim any session in the environment, and your Prized key stay in this process. The worker in the sandbox runs on the work item's secret alone, and the secret is deleted when the worker exits.
- **A handoff that fails** (Prized refuses the sandbox, the download doesn't match its sha256, the worker dies at once) is not acknowledged, so the queue hands it out again after the reclaim window, twice `HANDOFF_TIMEOUT`. A sandbox made for the failed try is deleted; one holding the session's files is paused. After 3 failed tries the work item is stopped instead, so it doesn't come back forever.
- **While a run goes on,** the orchestrator checks each sandbox's workers every 15 seconds, and those checks keep the sandbox awake. If the orchestrator goes away, its sandboxes pause themselves after `AUTO_PAUSE`, freezing the runs rather than killing them; a restarted orchestrator finds them by their labels and carries on.
- **When a run ends** (the worker exits a minute after the session goes idle), the sandbox pauses, memory and all, until the session's next message. A run past `RUN_TIMEOUT` is stopped.
- **When the session ends** (`session.status_terminated` or `session.deleted`, confirmed with the API), its sandbox is deleted. A sweep every 5 minutes deletes any whose session ended while no one was watching. A paused sandbox whose session never comes back is deleted after `KEEP_PAUSED`.

## Start sessions from a prepared sandbox

Set up a sandbox once (repositories cloned, dependencies installed) and every new session starts as a live copy of it:

```bash
prized-managed-agents prepare --setup "git clone https://github.com/you/app && cd app && npm ci"
export WARM_PARENT=sbx_…   # the id it prints
```

The parent stays paused, with `ant` installed. Copies keep the parent's `AUTO_PAUSE` and `KEEP_PAUSED`, so set those when you run `prepare`.

A session can choose for itself through its [metadata](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-workers#stage-files-for-a-session): `prized_template` picks the template, and `prized_fork_from` names a warm parent to fork. Only sandboxes `prepare` made (labelled `prized-managed-agents-role=warm-parent`) can be forked this way. `SESSION_OVERRIDES=0` turns both off.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `PRIZED_TEMPLATE` | `prized-base-v1` | The sandbox template. `prized-agent-v1` adds Claude Code and Codex |
| `PRIZED_VCPU`, `PRIZED_MEMORY_GB`, `PRIZED_DISK_GB` | The template's | Each session's shape |
| `WARM_PARENT` | | A paused sandbox from `prepare` to fork new sessions from |
| `PRIZED_LABELS` | | Extra labels on every sandbox, `team=infra,cost=agents` |
| `KEEP_PAUSED` | `7d` | How long a paused session's sandbox waits for its next run before it is deleted (up to `28d`; `0` keeps it) |
| `MAX_RUNTIME` | Prized's, `24h` | A sandbox's run time, restarting on every resume (up to `7d` on a paid plan) |
| `AUTO_PAUSE` | `15m` | If the orchestrator stops, its sandboxes pause after this long without it |
| `MAX_IDLE` | `60s` | How long the worker stays after the session goes idle (`ant --max-idle`) |
| `RUN_TIMEOUT` | `6h` | The longest one run may take before its worker is stopped |
| `HANDOFF_TIMEOUT` | `120s` | How long a handoff may take; an item that fails comes back after twice this |
| `MAX_HANDOFFS`, `MAX_SESSIONS` | `8`, `50` | Sandboxes being prepared at once, and runs served at once; past them, work waits in the queue |
| `RECONCILE_EVERY` | `60s` | With webhooks, how often the queue is polled anyway |
| `SESSION_OVERRIDES` | `1` | Whether session metadata may pick the template or a warm parent |
| `ANT_VERSION`, `ANT_SHA256_AMD64`, `ANT_SHA256_ARM64` | `2.7.1` and its sha256s | Another `ant` release, with the sha256s of its `linux_amd64` and `linux_arm64` tarballs from its `checksums.txt` |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Another API endpoint; passed to the worker too |
| `HOST`, `PORT` | `127.0.0.1`, `8787` | Where the receiver listens: `POST /webhook`, `GET /healthz` |

## Limits

- **Working directory** `/home/prized/work`, not `/workspace`. Tools run as the user `prized`, without root. If your agent uses skills, say so in its system prompt, as Anthropic's [sandbox filesystem](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-reference#sandbox-filesystem) notes ask for any other directory.
- **Memory stores** need a writable `/mnt/memory`, which a sandbox doesn't have yet. A session with memory stores attached fails when its run starts; the end of the worker's log, printed by the orchestrator, says `cannot create the memory store's folder`.
- **Network.** Sandboxes have open outbound internet; there are no per-session egress rules yet.
- **The receiver** must be reachable at a public HTTPS URL on 443 for webhooks. A Prized sandbox can't host it: a public port doesn't wake a paused sandbox. Without one, run with `--poll`.
- **Files** last while the session's sandbox does: if it is deleted (after `KEEP_PAUSED`, at the end of its run time, or if its host fails), the next run starts from an empty working directory.
- **Quotas.** 50 running sandboxes per workspace by default; on free credit, 10 at once and 60 minutes of running per sandbox. Paused sandboxes keep their disk: on a paid plan, paused storage past 20 GB costs $0.05 per GB-month ([Pause and resume](https://prized.dev/docs/sandboxes?via=claude-managed-agents-orchestrator#pause-and-resume)).

## Troubleshooting

- **A session stays queued.** `prized-managed-agents status` shows the queue. No orchestrator is running, every slot is busy (`MAX_SESSIONS`), or each handoff fails: the log says why (`handoff failed`, then `gave up`).
- **Webhooks don't arrive.** The Console shows the endpoint's state; a redirect (3xx) or a private address disables it. Sessions still start within `RECONCILE_EVERY`.
- **The worker's own log** is in the sandbox: `prized sandbox exec sbx_… -- "tail ~/.prized-managed-agents/runs/*/worker.log"`. When a run fails, the orchestrator prints its end.

## Tests

```bash
scripts/test.sh claude-managed-agents-orchestrator   # from the repo root: lint + offline tests
```

The tests run the orchestrator against a stand-in for Anthropic's side built from the API reference: the work queue with its reclaim window and leases (heartbeats with optimistic concurrency, a lapsed lease goes back to the queue), the session endpoint, and webhooks signed with the Standard Webhooks library and retried as Anthropic retries them. Sandboxes are fake but their commands really run, so the in-sandbox scripts and the `ant` install are tested as they are, with a stub `ant` that speaks the lease protocol on the work item's token alone. They check the ack comes only after the worker started; that the environment key never reaches a sandbox (no command's environment, no file); that a follow-up resumes the same sandbox with its files; that ended sessions lose their sandboxes, by webhook and by sweep; that forged, stale and duplicate deliveries do nothing; that a failed handoff is left for the queue, cleans up and succeeds when the item comes back, and gives up after 3 tries; that a run longer than `AUTO_PAUSE` stays awake while one whose orchestrator died pauses; that a restarted orchestrator adopts running work; and the signature check against the reference library on 300 random deliveries.

Prized Sandboxes and Claude Managed Agents are both in beta. Apache-2.0.
