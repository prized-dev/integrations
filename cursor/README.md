# Cursor Cloud Agents in Prized Sandboxes

Run each [Cursor Cloud Agent](https://cursor.com/docs/cloud-agent)'s tools in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=cursor) of its own, through a [Team Pool](https://cursor.com/docs/cloud-agent/self-hosted/pool). Cursor runs the agent loop. The sandbox runs the agent's terminal commands, file edits and git, and is deleted when the agent is done.

Team Pools need a Cursor Enterprise plan. On any plan with Cloud Agents, you can run agents on a Prized box instead: [Run Cursor Cloud Agents on Prized](https://prized.dev/docs/cursor-cloud-agents?via=cursor).

## Run it

1. In Cursor's [Cloud Agents dashboard](https://cursor.com/dashboard/cloud-agents#self-hosted), a team admin turns on **Allow Self-Hosted Machines** and GitHub token minting for Team Pool workers. Then create a service account under **Settings → API Keys → Service Accounts** and copy its key.

2. On a machine that stays on (Linux or macOS), install the Cursor CLI and the `prized` CLI:

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
- The worker waits 10 minutes after the agent's last turn for a follow-up, then exits, and the sandbox is deleted. A later follow-up starts a fresh sandbox.
- The controller and `spawn.sh`'s watchers run on your machine: keep it up under systemd or tmux. If it stops, agents still running lose their worker, and their sandboxes pause 5 minutes later and are deleted 10 minutes after that.
- A follow-up after the idle window gets a new worker. Keep the pool's reconnect window (`workerReadyTimeoutSeconds`) at 0, its default.
- Nothing connects in: the controller and the workers call out to Cursor over HTTPS.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `PRIZED_TEMPLATE` | `prized-base-v1` | The sandbox's template. One with the Cursor CLI and your toolchains installed starts faster ([own template](https://prized.dev/docs/sandboxes-own-template?via=cursor)) |
| `MAX_RUNTIME` | Prized's default | The most one sandbox runs: `24h` unless set, up to `7d` on a paid plan; 60 minutes in all on the free tier |
| `CURSOR_IDLE_RELEASE` | `600` | Seconds a worker waits for a follow-up before it exits |
| `CURSOR_WORKER_FLAGS` | `--clone-git-repos` | The `agent worker` flags beside `--pool` and `--worker-id`. Setting it replaces the default: `--clone-git-repos --connect-workspace` |
| `PRIZED_CURSOR_LOGS` | `~/.local/state/prized-cursor` | Where each request's log and its worker's log go |

## Keys in the sandbox

The service account's key stays on the controller's machine. With `--session-token`, each sandbox gets only a session token for its own claim, in `~/.cursor-worker/token`, readable by the agent's tool calls. It stops working when the claim ends. `spawn.sh` refuses to run without one. Never pass your Prized API key into a sandbox.

## Limits

- **Pool name.** Any name but `default`: `--clone-git-repos` works only on a named any-repo pool.
- **Working directory** `/home/prized/work`. Tools run as the user `prized`, without root.
- **No hibernation.** A released worker's sandbox is deleted, not paused, so a follow-up after the idle window starts from a fresh checkout.
- **Not yet run in a real Team Pool.** Without an Enterprise service account, Cursor turns the pool worker away at sign-in. Up to that point `spawn.sh` has run against the real Prized API and Cursor. With its worker started as a My Machines worker instead, a Cursor agent ran end to end in the sandbox it created, and the sandbox was deleted once the worker exited.

## Test

```bash
test/test.sh
```

Runs `spawn.sh` against a stand-in `prized` and a stub `agent`, offline, with no keys.
