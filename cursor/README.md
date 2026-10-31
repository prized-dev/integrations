# Cursor Cloud Agents in Prized Sandboxes

Run each [Cursor Cloud Agent](https://cursor.com/docs/cloud-agent)'s tools in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=cursor) of its own, through a [Team Pool](https://cursor.com/docs/cloud-agent/self-hosted/pool). Cursor runs the agent loop. The sandbox runs the agent's terminal commands, file edits and git. Between jobs it is paused with its memory, so a follow-up finds the agent's workspace, and the servers it started, as they were.

Team Pools need a Cursor Enterprise plan. On any plan with Cloud Agents, you can run agents on a Prized box instead: [Run Cursor Cloud Agents on Prized](https://prized.dev/docs/cursor-cloud-agents?via=cursor).

## Run it

1. In Cursor's [Cloud Agents dashboard](https://cursor.com/dashboard/cloud-agents#self-hosted), a team admin turns on **Allow Self-Hosted Machines** and GitHub token minting for Team Pool workers. Then create a service account under **Settings → API Keys → Service Accounts** and copy its key.

2. On a machine that stays on (Linux or macOS), install the Cursor CLI and the `prized` CLI, and `jq` if it isn't there:

   ```bash
   curl https://cursor.com/install -fsS | bash
   curl -fsSL https://prized.dev/install.sh | sh
   ```

3. Make a Prized API key with **Sandboxes: Write** at https://prized.dev/dashboard/workspace/api-keys?via=cursor, and export both keys there:

   ```bash
   export CURSOR_API_KEY=…   # the service account's key
   export PRIZED_API_KEY=dcp_…
   ```

4. Start the controller:

   ```bash
   git clone https://github.com/prized-dev/integrations
   cd integrations/cursor
   agent worker controller --spawn "$PWD/spawn.sh" --session-token --pool prized
   ```

5. At [cursor.com/agents](https://cursor.com/agents), pick the **prized** pool under **Any repo**, choose the repository as well (a run without one has nothing to clone), and send a task. From the API, create the agent with `"env": {"type": "pool", "name": "prized"}` and `"repos": [{"url": "https://github.com/you/repo"}]`. `prized sandbox ls` shows its sandbox, labelled `cursor-worker=…`.

## What happens

- The controller claims each request and runs `spawn.sh`, which creates a sandbox from `prized-base-v1`, installs the Cursor CLI there, and starts `agent worker --pool prized` under the worker id Cursor assigned. It returns at once, so the controller claims the next request while this one starts.
- With `--clone-git-repos`, the worker checks out the agent's repositories with a short-lived GitHub token Cursor mints for the run.
- The worker waits 10 minutes after the agent's last turn for a follow-up, then exits, and the sandbox is paused ([Workers pause between jobs](#workers-pause-between-jobs)).
- The controller and `spawn.sh`'s watchers run on your machine: keep it up under systemd or tmux. If it stops, agents still running lose their worker, and their sandboxes pause 5 minutes later.
- Nothing connects in: the controller and the workers call out to Cursor over HTTPS.

## Workers pause between jobs

When a worker releases after its idle window, `spawn.sh` pauses its sandbox instead of deleting it. Its memory, running processes and files are kept, and it bills storage only: a workspace's first 20 GB are included, then $0.05 per GB-month (a 4 vCPU, 8 GB sandbox holds 18 GB paused). A follow-up for that agent comes back as a wake (`CURSOR_WAKE=1`). `spawn.sh` resumes the same sandbox, hands it the wake's session token and starts the worker again under the same id.

For Cursor to send the wake instead of handing the follow-up to a fresh worker, give the pool a reconnect window, with the service account's key:

```bash
curl https://api.cursor.com/v0/private-workers/pools -u "$CURSOR_API_KEY:" -H 'Content-Type: application/json' \
  -d '{"scope": "team", "poolName": "prized", "workerReadyTimeoutSeconds": 900}'
```

- A paused worker is deleted by Prized after `CURSOR_HIBERNATE_TTL` (3 days unless set), even while this machine is down. `./reap.sh --older-than 1d --pool prized` deletes them sooner.
- Each resume starts the sandbox's run time again (`MAX_RUNTIME`). Time added with `prized sandbox extend` lasts only until the next pause.
- Paused sandboxes count toward the workspace's disk quota. When a new agent's sandbox doesn't fit, `spawn.sh` deletes the pool's longest-paused worker to make room.
- A wake with nothing to resume starts a fresh sandbox under the same worker id. A worker that fails is deleted, not paused.
- `CURSOR_HIBERNATE=0` deletes each sandbox when its worker exits, as earlier versions did.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `PRIZED_TEMPLATE` | `prized-base-v1` | The sandbox's template. One with the Cursor CLI and your toolchains installed starts faster ([own template](https://prized.dev/docs/sandboxes-own-template?via=cursor)) |
| `MAX_RUNTIME` | Prized's default | The most one sandbox runs from its create or a resume: `24h` unless set, up to `7d` on a paid plan; 60 minutes in all on the free tier |
| `CURSOR_IDLE_RELEASE` | `600` | Seconds a worker waits for a follow-up before it exits |
| `CURSOR_WORKER_FLAGS` | `--clone-git-repos` | The `agent worker` flags beside `--pool` and `--worker-id`. Setting it replaces the default: `--clone-git-repos --connect-workspace` |
| `CURSOR_HIBERNATE` | `1` | `1` pauses a released worker's sandbox for its follow-ups; `0` deletes it |
| `CURSOR_HIBERNATE_TTL` | `3d` | How long a paused worker is kept before Prized deletes it, up to `28d` |
| `PRIZED_CURSOR_LOGS` | `~/.local/state/prized-cursor` | Where each request's log and its worker's log go |

## Keys in the sandbox

The service account's key stays on the controller's machine. With `--session-token`, each sandbox gets only a session token for its own claim, in `~/.cursor-worker/token`, readable by the agent's tool calls. It stops working when the claim ends. `spawn.sh` refuses to run without one. Never pass your Prized API key into a sandbox.

## Limits

- **Pool name.** Any name but `default`: `--clone-git-repos` works only on a named any-repo pool.
- **Working directory** `/home/prized/work`. Tools run as the user `prized`, without root.
- **Not yet run in a real Team Pool.** Without an Enterprise service account, Cursor turns the pool worker away at sign-in. Up to that point `spawn.sh` has run against the real Prized API and Cursor. With its worker started as a My Machines worker instead, a Cursor agent ran end to end in the sandbox it created, and the sandbox was deleted once the worker exited. Pausing and waking have run against the real Prized API with a stand-in worker, not yet with Cursor's wake. Still to see there: whether a woken worker checks out the repositories again. With `--clone-git-repos` it reuses a clean checkout, and clones a fresh copy beside one with uncommitted changes.

## Test

```bash
test/test.sh
```

Runs `spawn.sh` and `reap.sh` against a stand-in `prized` and a stub `agent`, offline, with no keys.
