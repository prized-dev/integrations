# Claude Managed Agents on Prized: the webhook orchestrator

> **Not on PyPI yet.** Install it from this repo. Tested offline against a stand-in for Anthropic's API (see Tests); not yet run against a live environment.

Serve a [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) [self-hosted environment](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes) with [Prized sandboxes](https://prized.dev/docs/sandboxes?via=claude-managed-agents-orchestrator): each session gets a Firecracker microVM of its own. Claude runs on Anthropic's side; the sandbox runs the agent's tools and holds its files. A `session.status_run_started` webhook starts the work, and between runs the sandbox pauses with its memory, files and processes, so the session's next message carries on where it stopped, about a second later. When the session ends, its sandbox is deleted.

For one poller on a machine you keep running, with nothing to install but `ant` and `jq`, use [`on-work.sh`](../claude-managed-agents) instead.

## Run it

1. In the [Claude Console](https://platform.claude.com/environments), create a self-hosted environment and generate its environment key. Under [Webhooks](https://platform.claude.com/settings/workspaces/default/webhooks), add an endpoint (HTTPS on port 443, a public hostname) for `session.status_run_started`, `session.status_terminated`, `session.status_idled` and `session.deleted`, and copy its signing secret.

2. Make a Prized API key with **Sandboxes: Write** at https://prized.dev/dashboard/workspace/api-keys?via=claude-managed-agents-orchestrator. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=claude-managed-agents-orchestrator.

3. On a machine that stays on, install it and export the four values:

   ```bash
   python3 -m venv ~/.pma && ~/.pma/bin/pip install "git+https://github.com/prized-dev/integrations#subdirectory=claude-managed-agents-orchestrator"
   export PATH="$HOME/.pma/bin:$PATH"
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

   Run one per environment. To restart or upgrade, stop the old process before starting the new one: it picks up the running work, while two at once would pause sandboxes the other is starting.

5. Start a session on the environment and send it a message. `prized sandbox ls` shows its sandbox, labelled `anthropic-session=sesn_…`; `prized-managed-agents status` shows the queue and each sandbox's runs.

## What happens

- **A webhook arrives.** Its signature is checked (Standard Webhooks, as Anthropic's SDKs do, within 5 minutes) and retries of it are dropped. A run-started event wakes the intake, which claims everything queued. The queue, not the webhook, is the record: every minute it polls once more anyway, since a lost webhook is never sent again.
- **A work item is claimed.** The session's sandbox is resumed if it has one (paused, with its files and processes). Otherwise a warm parent is forked, or a sandbox is created from `prized-base-v1`. A worker still in a resumed sandbox (frozen when it paused itself) is stopped first, so two never share it. Anthropic's worker, `ant` 2.7.1, is installed in it if missing, checked against its published sha256. The work item's secret is written into the sandbox, readable by its user only, and `ant beta:worker run` starts. Only then is the item acknowledged.
- **The secret reaches one session.** The environment key, which can claim any session in the environment, and your Prized key stay in this process. The worker in the sandbox runs on the work item's secret alone, and the secret is deleted when the worker exits.
- **The sandbox can't make up runs.** The agent's tools can write anything in the sandbox, including the files the orchestrator reads to see its workers. So a run counts only if its id carries a signature made with a key derived from the environment key, and only for the sandbox's own session: anything else there is ignored, and can't take a slot, keep the sandbox awake, or touch another session. A run of its own that the agent revives (a collected run, or one past `RUN_TIMEOUT`) is killed rather than adopted.
- **A handoff that fails** (Prized refuses the sandbox, the download doesn't match its sha256, the worker dies at once) is not acknowledged, so the queue hands it out again after the reclaim window, twice `HANDOFF_TIMEOUT`. A sandbox made for the failed try is deleted; one holding the session's files is paused. After 3 failed tries the work item is stopped instead, so it doesn't come back forever. Once the worker has started, it is never stopped on a hunch: a check that fails then leaves it running, and only the sandbox saying it exited counts as a failure.
- **While a run goes on,** the orchestrator checks each sandbox's workers every 15 seconds, and those checks keep the sandbox awake. If the orchestrator goes away, its sandboxes pause themselves after `AUTO_PAUSE`, freezing the runs rather than killing them; a restarted orchestrator finds them by their labels and carries on.
- **When a run ends** (the worker exits a minute after the session goes idle), the sandbox pauses, memory and all, until the session's next message, once a check shows no other worker runs in it. A run past `RUN_TIMEOUT` (counted from its start, across restarts) is stopped.
- **When the session ends** (`session.status_terminated` or `session.deleted`, confirmed with the API), its sandbox is deleted. A sweep every 5 minutes deletes any whose session ended while no one was watching. A paused sandbox whose session never comes back is deleted after `KEEP_PAUSED`.

## Start sessions from a prepared sandbox

Set up a sandbox once (repositories cloned, dependencies installed) and every new session starts as a live copy of it:

```bash
prized-managed-agents prepare --setup "git clone https://github.com/you/app && cd app && npm ci"
export WARM_PARENT=sbx_…   # the id it prints
```

The parent stays paused, with `ant` installed, until you retire it: `prized-managed-agents retire` lists this environment's parents, and `prized-managed-agents retire sbx_…` deletes one (sessions forked from it keep their copies). Set `WARM_PARENT_KEEP` before `prepare` to have it deleted after that long paused instead. Copies keep the parent's `AUTO_PAUSE`, so set that when you run `prepare`; they would inherit its deletion timer too, so the sweep deletes a copy once it has been paused for `KEEP_PAUSED`. On the free tier Prized deletes any sandbox 30 days after its last use, a parent too.

A session can choose for itself through its [metadata](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-workers#stage-files-for-a-session): `prized_template` picks the template, and `prized_fork_from` names a warm parent to fork. Only warm parents `prepare` made for this environment (labelled `prized-managed-agents-role=warm-parent`, with the environment's id and your `PRIZED_LABELS`) can be forked, this way or as `WARM_PARENT`. `SESSION_OVERRIDES=0` turns both off.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `PRIZED_TEMPLATE` | `prized-base-v1` | The sandbox template. `prized-agent-v1` adds Claude Code and Codex |
| `PRIZED_VCPU`, `PRIZED_MEMORY_GB`, `PRIZED_DISK_GB` | The template's | Each session's shape |
| `WARM_PARENT` | | A paused sandbox from `prepare` to fork new sessions from |
| `WARM_PARENT_KEEP` | `0` | How long a warm parent from `prepare` waits, paused, before it is deleted; `0` keeps it until it is retired |
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
| `ANT_URL` | GitHub's release URL | Where to download `ant` from (a mirror), with `{version}` and `{arch}` in it; the sha256 check still applies |
| `PROBE_EVERY`, `SWEEP_EVERY` | `15s`, `5m` | How often running sandboxes are checked (keep it well under `AUTO_PAUSE`), and how often ended sessions' sandboxes are swept |
| `ORCH_ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Another API endpoint, https only (or http to this machine); passed to the worker too. `ANTHROPIC_BASE_URL` is never read, since Claude Code and Anthropic's SDKs set it for gateways and proxies: the start says which URL the environment key goes to, and warns when `ANTHROPIC_BASE_URL` is set |
| `FREE_TIER` | `auto` | Whether the workspace is on Prized's free tier: `auto` asks Prized at start (it can tell from a key that can read billing, or once the free tier has ended a sandbox), `1` says it is, `0` that it isn't. See [On the free tier](#on-the-free-tier) |
| `EVICT_PAUSED` | `1` | On the free tier, when the disk is full, delete the session sandbox paused longest so a new session can start |
| `HOST`, `PORT` | `127.0.0.1`, `8787` | Where the receiver listens: `POST /webhook`, `GET /healthz` |
| `ANTHROPIC_ENVIRONMENT_KEY_PREVIOUS` | | After rotating the environment key, the old one, so runs started under it are still recognised (their ids are signed with a key derived from it). Used for nothing else |

## Limits

- **Working directory** `/home/prized/work`, not `/workspace`. Tools run as the user `prized`, without root. If your agent uses skills, say so in its system prompt, as Anthropic's [sandbox filesystem](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes-reference#sandbox-filesystem) notes ask for any other directory.
- **Memory stores** need a writable `/mnt/memory`, which a sandbox doesn't have yet. A session with memory stores attached fails when its run starts; the end of the worker's log, printed by the orchestrator, says `cannot create the memory store's folder`.
- **Network.** Sandboxes have open outbound internet; there are no per-session egress rules yet.
- **The receiver** must be reachable at a public HTTPS URL on 443 for webhooks. A Prized sandbox can't host it: a public port doesn't wake a paused sandbox. Without one, run with `--poll`.
- **Files** last while the session's sandbox does: if it is deleted (after `KEEP_PAUSED`, at the end of its run time, or if its host fails), the next run starts from an empty working directory.
- **Quotas.** 50 running sandboxes per workspace by default; on free credit, 10 at once and 60 minutes of running per sandbox ([On the free tier](#on-the-free-tier)). Paused sandboxes keep their disk: on a paid plan, paused storage past 20 GB costs $0.05 per GB-month ([Pause and resume](https://prized.dev/docs/sandboxes?via=claude-managed-agents-orchestrator#pause-and-resume)).

## On the free tier

A workspace on its free credit alone runs 10 sandboxes at once, each with 60 minutes of running in all, and has 100 GB of disk across its sandboxes, paused ones included ([Free tier](https://prized.dev/docs/sandboxes?via=claude-managed-agents-orchestrator#free-tier)). When it is on the free tier (`FREE_TIER`), the orchestrator says so at start, and:

- **Changes the defaults** you haven't set: `MAX_SESSIONS` 10 (always: more would be refused), `MAX_IDLE` 10s, so each message spends its turn and little more of the 60 minutes, and `AUTO_PAUSE` 2m. A `MAX_RUNTIME` over 60 minutes is dropped, since Prized would refuse it.
- **Makes room.** When Prized refuses a new session's sandbox because the disk is full, the session sandbox paused longest is deleted, with its files, and the log says `evicted` and whose. A refusal that says `"tier": "free"` turns this on even when `FREE_TIER` couldn't tell. `EVICT_PAUSED=0` turns it off, and then new sessions fail until a paused one is deleted.
- **Names lost sandboxes.** When a session's sandbox used up its 60 minutes, Prized deleted it, memory and files with it. The log says `sandbox gone` with Prized's reason, mid-run or at the session's next message, which starts in a new sandbox. A sandbox with under 10 minutes of running left is named (`run time low`) when its session comes back.

A top-up or a plan lifts all of this; restart with `FREE_TIER=0` (or let `auto` see it).

## Troubleshooting

- **A session stays queued.** `prized-managed-agents status` shows the queue. No orchestrator is running, every slot is busy (`MAX_SESSIONS`), or each handoff fails: the log says why (`handoff failed`, then `gave up`).
- **`HTTP 401` at start** (`is ANTHROPIC_ENVIRONMENT_KEY this environment's key?`): the key is wrong, revoked or for another environment. The line before it says where the key went; only `ORCH_ANTHROPIC_BASE_URL` changes that.
- **Webhooks don't arrive.** The Console shows the endpoint's state; a redirect (3xx) or a private address disables it. Sessions still start within `RECONCILE_EVERY`.
- **The worker's own log** is in the sandbox: `prized sandbox exec sbx_… -- "tail ~/.prized-managed-agents/runs/*/worker.log"`. When a run fails, the orchestrator prints its end.
- **A run shows `"genuine": false`** in `prized-managed-agents status`: something in the sandbox wrote it, and the orchestrator ignores it. Runs started before you rotated the environment key show this too, unless you restart with the old key in `ANTHROPIC_ENVIRONMENT_KEY_PREVIOUS` until they have ended.

## Tests

```bash
scripts/test.sh claude-managed-agents-orchestrator   # from the repo root: lint + offline tests
```

The tests run the orchestrator against a stand-in for Anthropic's side built from the API reference: the work queue with its reclaim window and leases (heartbeats with optimistic concurrency, a lapsed lease goes back to the queue), the session endpoint, and webhooks signed with the Standard Webhooks library and retried as Anthropic retries them. Sandboxes are fake but their commands really run, so the in-sandbox scripts and the `ant` install are tested as they are, with a stub `ant` that speaks the lease protocol on the work item's token alone. They check the ack comes only after the worker started; that the environment key never reaches a sandbox (no command's environment, no file); that a follow-up resumes the same sandbox with its files; that ended sessions lose their sandboxes, by webhook and by sweep; that forged, stale and duplicate deliveries do nothing; that a failed handoff is left for the queue, cleans up (even when a cleanup step fails) and succeeds when the item comes back, and gives up after 3 tries; that a run longer than `AUTO_PAUSE` stays awake while one whose orchestrator died pauses; that a restarted orchestrator adopts running work, never pauses a sandbox whose worker is alive, and keeps each run's start time; that a worker frozen in a resumed sandbox is stopped before the next one starts; that a lost status check or launch answer leaves a healthy worker alone; that an item that comes again after a lost ack gets a new worker if the old one died; that runs an agent forges in its sandbox (signed with nothing, smuggled in by a newline, named for another session's work) add no run, take no slot and reach no other session; that only this environment's warm parents can be forked, a warm parent is kept until it is retired, and its copies are deleted once paused past `KEEP_PAUSED`; that the environment key never follows `ANTHROPIC_BASE_URL` and goes only to https (or this machine); that on the free tier the defaults change, a full disk deletes the session sandbox paused longest (and nothing off the free tier), and a sandbox the free tier deleted is named with Prized's reason; and the signature check against the reference library on 300 random deliveries.

Prized Sandboxes and Claude Managed Agents are both in beta. Apache-2.0.
