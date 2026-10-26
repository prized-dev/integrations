# Claude Managed Agents in Prized Sandboxes

Run each [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) session's tools in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=claude-managed-agents) of its own, through a [self-hosted environment](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes). Claude runs on Anthropic's side. The sandbox runs the agent's commands and holds its files, and between messages it pauses with its files and processes, so the session's next message carries on where it stopped.

## Run it

1. On a machine that stays on (Linux or macOS), install [`ant`](https://github.com/anthropics/anthropic-cli/releases) 2.3.0 or later, `jq`, and the `prized` CLI:

   ```bash
   curl -fsSL https://prized.dev/install.sh | sh
   ```

2. In the [Claude Console](https://platform.claude.com/workspaces/default/environments), create a self-hosted environment and generate its environment key. Make a Prized API key with **Sandboxes: Write** at https://prized.dev/dashboard/workspace/api-keys?via=claude-managed-agents.

3. Export the three keys on that machine (`PRIZED_API_KEY` is required: the script calls Prized's API directly to resume a sandbox):

   ```bash
   export ANTHROPIC_ENVIRONMENT_ID=env_…
   export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-…
   export PRIZED_API_KEY=dcp_…
   ```

4. Start a poller:

   ```bash
   git clone https://github.com/prized-dev/integrations
   cd integrations/claude-managed-agents
   ant beta:worker poll --on-work ./on-work.sh
   ```

   A poller serves one session at a time. For four sessions at once, start four (Ctrl-C stops them all):

   ```bash
   bash -c 'for i in 1 2 3 4; do ant beta:worker poll --on-work ./on-work.sh & done; wait'
   ```

5. Start a session on the environment and send it a message. `prized sandbox ls` shows its sandbox, labelled `anthropic-session=sesn_…`.

## What happens

- The poller claims the session's work and runs `on-work.sh`. It resumes the session's sandbox, or creates one from `prized-base-v1`, and installs `ant` there if it's missing (the download is checked against its sha256).
- `ant beta:worker run` starts in the sandbox with only the work item's secret, which reaches that one session. The environment key, which can claim any session in the environment, stays on the poller's machine. A work item without a secret is refused.
- When the run ends, the worker exits and the poller takes the next work item. If the session ended its turn, that is a minute after it went idle. A session waiting on anything else, such as a tool call you must approve, keeps the worker running until `RUN_TIMEOUT` stops it.
- Nobody pauses the sandbox: it pauses itself after 2 minutes without a command, keeping its files and processes, and the session's next message resumes it. Each work item keeps its own files in the sandbox, so another poller can pick up the next message while this run is still finishing, and neither touches the other's worker.
- Nothing connects in: the poller and the worker call out to `api.anthropic.com` over HTTPS.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `KEEP_PAUSED` | `1d` | How long a session's sandbox waits, paused, for its next message before it is deleted (up to `28d`) |
| `RUN_TIMEOUT` | `2h` | The longest one run may take before its worker is stopped (`90m`, `600s`) |
| `MAX_RUNTIME` | Prized's default | Each sandbox's run time, which restarts on every resume: `24h` unless set, up to `7d` on a paid plan; 60 minutes in all on the free tier |
| `PRIZED_TEMPLATE` | `prized-base-v1` | The sandbox's template. `prized-agent-v1` adds Claude Code and Codex |
| `ANT_VERSION`, `ANT_SHA256` | `2.3.0` and its sha256 | The `ant` release installed in the sandbox. Set both to use another: the sha256 of its `linux_amd64.tar.gz` is in the release's `ant_<version>_checksums.txt` |

## Limits

- **Working directory** `/home/prized/work`. Tools run as the user `prized`, without root, so there is no `/workspace`.
- **Skills.** A worker that holds only the session's secret can't download skills (Anthropic's skills API takes the environment key), so it logs `failed to download skill` and carries on.
- **Memory stores** need a writable `/mnt/memory`, which a sandbox doesn't have. A session with memory stores attached fails as soon as its run starts; the poller prints the end of the worker's log, which says `cannot create the memory store's folder`.
- **Files** last while the session's sandbox does. If it is deleted (after `KEEP_PAUSED`, at the end of its run time, or if its machine fails), the next message starts from an empty working directory.
- **Paused sandboxes keep their disk**, 10 GB each on `prized-base-v1`, for `KEEP_PAUSED`. On the free tier, 100 GB of disk across the workspace's sandboxes holds about ten recently active sessions; on a paid plan, paused storage past 20 GB costs $0.05 per GB-month ([Pause and resume](https://prized.dev/docs/sandboxes?via=claude-managed-agents#pause-and-resume)).
- **Run time.** A sandbox is deleted after 24 hours of running unless you set `MAX_RUNTIME` (up to `7d` on a paid plan); the clock restarts on each resume. On the free tier, 60 minutes of running per sandbox in all, and the 2 idle minutes before each pause count toward it ([Limits](https://prized.dev/docs/limits?via=claude-managed-agents#sandboxes)).
- **If the poller's machine goes away** mid-run, the sandbox pauses itself 2 minutes later.

## Troubleshooting

- **A session stays queued.** No poller is running, or every poller is busy with a session: start another.
- **The poller prints a `prized:` error.** Prized refused the sandbox, for example at the free tier's limits; the line after it says what to change.
- **A resume is refused** for a reason that may pass (a quota, the balance, no host that fits its shape right now), so the work item fails after 3 tries and the sandbox is kept, files and all. Only when the sandbox can't come back (its run time is used up, or it failed or is gone) does the session get a new one.
- **The worker's own log** is in the sandbox: `prized sandbox exec sbx_… -- cat ~/.anthropic/<work id>/worker.log`. When a run fails, the poller prints its last lines.

## Test

```bash
ANT=/path/to/ant test/test.sh
```

It runs `on-work.sh` under the real `ant beta:worker poll --on-work`, against a stand-in for Anthropic's work queue and a fake `prized`, with no network and no real sandboxes. It checks that the poller holds each work item until its run ends; that a follow-up resumes the same sandbox with its files; that only the session's secret enters the sandbox; that a stopped poller (even Ctrl-C plus SIGTERM) stops its worker; that two pollers on one session's consecutive messages never stop or freeze each other's workers (the fake pause freezes processes, as a real one does); that a failed download and a binary with the wrong sha256 are refused and recovered from; that a resume refused for a quota or for host capacity is retried and then fails the work item, while one refused for max_runtime gets a new sandbox; and that a dying worker and a run past `RUN_TIMEOUT` end the wait.

Prized Sandboxes and Claude Managed Agents are both in beta. Apache-2.0.
