# Amp orbs in Prized Sandboxes

Run each [Amp](https://ampcode.com) orb in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=amp-orbs) of its own, as a [self-hosted orb provider](https://ampcode.com/docs/orbs/self-hosted). Amp runs the agent loop and the models. The sandbox, a Firecracker microVM, runs the agent's tools, holds the repository, and pauses with its memory when the thread goes idle, so the dev servers and processes it left running are still there when the next message wakes it.

## Run it

1. In Amp, open [Settings → Orbs](https://ampcode.com/settings/orbs) and click **Add Provider** (for a provider only you use), or **Add Orb Provider** in your workspace's settings (an admin, for everyone). Name it `prized` and copy the `aop_live_…` token. It is shown once.

2. Make a Prized API key with **Sandboxes: Write** at https://prized.dev/dashboard/workspace/api-keys?via=amp-orbs. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=amp-orbs.

3. On a machine that stays on (Linux or macOS, with bash, curl and jq), install the Amp CLI and export both keys:

   ```bash
   curl -fsSL https://ampcode.com/install.sh | bash
   export AMP_ORB_PROVIDER_TOKEN=aop_live_…
   export PRIZED_API_KEY=dcp_…
   ```

4. Start the provider, with the absolute path of the dispatch script:

   ```bash
   git clone https://github.com/prized-dev/integrations
   amp orb provider "$PWD/integrations/amp-orbs/dispatch.sh"
   ```

   Run exactly one provider per token, and keep it running under systemd, launchd or tmux.

5. In the new thread composer, click **Orb**, open the type dropdown and choose **prized** under **Self-Hosted**, or from the CLI:

   ```bash
   amp -ox "Run the tests and fix what fails" --orb-provider prized
   ```

   `prized sandbox ls` shows the orb's sandbox, labelled `amp-orb=amp-T-…`.

## What happens

| Amp sends | `dispatch.sh` does |
| --- | --- |
| `spawn`, new thread | Creates a sandbox from `prized-base-v1`, labelled `amp-orb=$ORB_ID`, installs the Amp CLI there with Amp's installer, and starts `amp orb bootstrap` detached, with the job's `ORB_TOKEN` |
| `pause`, thread idle or archived | Pauses the sandbox: memory, processes and disk kept, no compute billed |
| `spawn`, next message | Resumes the same sandbox, every process where it stopped, and starts `amp orb bootstrap` again with the new token |
| `delete`, thread deleted | Deletes the sandbox and its disk |

- **Measured** on 2026-10-28 and 29 against the real Prized API, on 2 vCPU, 4 GB sandboxes: a new orb's sandbox was running in 0.9 s and the whole `spawn` job took 4.4 to 4.8 s, the Amp CLI download included. A `pause` took 0.3 to 0.6 s. Across five resumes, after pauses of 20 seconds to 17 minutes, a resume took 0.3 to 0.8 s and the whole `spawn` job 0.7 to 1.6 s, and a 512 MB process and a counter loop carried on where they stopped.
- **Nothing is kept on this machine.** Each job finds its sandbox by the label, so a restarted provider picks up where the last left off.
- **Every job is safe to repeat** and ends within Amp's five minutes: each call waits only as long as the job has left. Answers that say nothing happened (rate limited, no capacity, not ready yet) are retried after their `Retry-After`; any other refusal ends the job with Prized's error. A create or fork carries an `Idempotency-Key`, so one whose answer is lost is sent again without making a second sandbox, and two jobs for one orb at once both end up on the older sandbox.
- **A sandbox in `error`** is deleted, and the orb starts afresh; Amp sets the repository up again. One that Prized reports as degraded first gets 30 seconds to answer again.
- **Nothing connects in.** The provider and the orb call out to Amp over HTTPS; the script calls Prized's API.

## Start new orbs warm

Set up one sandbox the way every orb should start (the Amp CLI installed, toolchains, caches), pause it, and new orbs start as live copies of it instead of from the template:

```bash
TPL=$(prized sandbox create --label role=amp-orb-template)
prized sandbox exec $TPL --timeout 10m -- 'curl -fsSL https://ampcode.com/install.sh | bash'
prized sandbox pause $TPL --wait
export PRIZED_ORB_FORK_FROM=$TPL
```

A copy keeps the template's shape, environment variables, auto-delete, files, memory and processes ([Fork](https://prized.dev/docs/sandboxes?via=amp-orbs#fork)), so the shape and auto-delete settings below apply only to orbs created from a template. Make the template without `--auto-pause`: copies inherit it, and it counts only API calls, not the agent's work, so `dispatch.sh` refuses one that has it, and one that is itself an orb's sandbox. When the template's machine has no room for a copy, the orb is created from the template's own template and shape instead.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `PRIZED_TEMPLATE` | `prized-base-v1` | The sandbox's template. `prized-agent-v1` adds Claude Code and Codex |
| `PRIZED_ORB_VCPU`, `PRIZED_ORB_MEMORY_GB`, `PRIZED_ORB_DISK_GB` | The template's default: 4 vCPU, 8 GB, 10 GB (2 vCPU and 4 GB on the free tier) | Each orb's shape |
| `PRIZED_ORB_MAX_RUNTIME_MIN` | Prized's default: 24 hours | The longest an orb runs from each wake before its sandbox is deleted, up to 10,080 (7 days) on a paid plan |
| `PRIZED_ORB_AUTO_DELETE_MIN` | Off | Delete an orb's sandbox after it has been paused this long. The next message starts it afresh |
| `PRIZED_ORB_LABELS` | None | More labels for each orb's sandbox: `team=web,env=dev` |
| `PRIZED_ORB_FORK_FROM` | None | A paused sandbox that new orbs start as copies of ([above](#start-new-orbs-warm)) |
| `PRIZED_API_URL` | `https://api.prized.dev/api/v1` | The Prized API |

## Keys in the sandbox

Your Prized API key stays on the provider's machine and never enters a sandbox. Each orb gets only the `ORB_TOKEN` of its `spawn` job, a short-lived credential for its own thread, in the environment of `amp orb bootstrap` (and, with `PRIZED_ORB_FORK_FROM`, any environment variables the template sandbox was created with). The token travels in the body of the API request, never on a command line.

## Limits

- **Not yet run with a real Amp thread.** Each job has run against the real Prized API, and `amp orb bootstrap` started in the sandbox and reached Amp (which turned down a made-up `ORB_TOKEN`); a thread on the provider, end to end, is next.
- **No root.** Tools run as the user `prized`, without sudo, in `/home/prized`. Amp's own orb image runs as `user` with passwordless sudo, which Amp's Desktop and `amp orb service` need, so expect those two not to work here.
- **No orb image.** Prized starts sandboxes from its own templates, so `ORB_IMAGE` is ignored and the Amp CLI is installed on the first `spawn` (or comes with `PRIZED_ORB_FORK_FROM`). The templates have no `tmux`, which Amp's Terminal tab uses.
- **No project snapshots.** `ORB_SNAPSHOT_ID` is ignored, so every new thread runs `.agents/setup`. `PRIZED_ORB_FORK_FROM` gives a head start.
- **Paused orbs are kept** until Amp deletes them with their thread, and count toward the workspace's paused storage ([Billing](https://prized.dev/docs/billing?via=amp-orbs#sandboxes)). Set `PRIZED_ORB_AUTO_DELETE_MIN` to drop old ones.
- **Keep the provider running.** If it stops, running orbs keep running, and billing, until Amp's `pause` reaches a provider again or their run time ends.
- **Free tier:** 60 minutes of running in all per sandbox.

## Test

```bash
test/test.sh
```

Runs `dispatch.sh` against a stand-in Prized API on 127.0.0.1 and a stub Amp CLI, with no keys and nothing beyond loopback. Needs bash, curl, jq and Python 3.
