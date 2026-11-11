# Gemini Managed Agents in Prized Sandboxes

Run the tools of [Gemini Managed Agents](https://aistudio.google.com/docs/agents) in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=gemini-managed-agents), through a [self-hosted environment](https://aistudio.google.com/docs/agent-environment#self-hosted-environments). Gemini and the agent loop run on Google's side. Each environment gets one sandbox, which runs the agent's shell commands and file edits. Between interactions it pauses with its files, installed packages and processes, and the environment's next interaction resumes it.

Status: tested offline against stand-ins for Google's poller and the Prized CLI ([Test](#test)). Not yet run against the Gemini API.

## Run it

Before you start, on a machine that stays on (Linux or macOS): `jq`, `curl`, the `prized` CLI v0.21.9 or later (`curl -fsSL https://prized.dev/install.sh | sh`), Google's [`gemini-api` CLI](https://github.com/google-gemini/gemini-api-cli) v0.4.6 or later, a [Gemini API key](https://aistudio.google.com/api-keys), and a Prized API key with **Sandboxes: Write** from https://prized.dev/dashboard/workspace/api-keys?via=gemini-managed-agents. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=gemini-managed-agents.

1. Get the script:

   ```bash
   git clone https://github.com/prized-dev/integrations && cd integrations/gemini-managed-agents
   ```

2. Create a self-hosted environment. The answer holds its `id` and its `environment_key`, which Google shows only this once:

   ```bash
   curl -fsS -X POST https://generativelanguage.googleapis.com/v1beta/environments \
     -H "x-goog-api-key: $GEMINI_API_KEY" -H "Content-Type: application/json" -d '{"mode": "self_hosted"}'
   ```

3. Export the environment's two values and your Prized key:

   ```bash
   export GEMINI_ENVIRONMENT_ID=… GEMINI_ENVIRONMENT_KEY=… PRIZED_API_KEY=dcp_…
   ```

4. Start Google's poller with the script as its provisioner:

   ```bash
   gemini-api environments poll --provision-cmd ./provision.sh
   ```

5. From another terminal, with `GEMINI_API_KEY` and `GEMINI_ENVIRONMENT_ID` set, run an interaction on the environment:

   ```bash
   gemini-api agent run "Create hello.txt and run uname -a in bash." \
     --agent antigravity-preview-09-2026 --body "{\"environment\": \"$GEMINI_ENVIRONMENT_ID\"}"
   ```

   `prized sandbox ls --label gemini-environment=$GEMINI_ENVIRONMENT_ID` shows the environment's sandbox.

## What happens

- The poller leases each interaction and runs `provision.sh` with the lease on stdin. The script resumes the environment's sandbox, or creates one from `prized-base-v1`, and installs the pinned `gemini-api` release there if it's missing (the download is checked against its sha256).
- `gemini-api environments serve` starts in the sandbox with only that interaction's lease token, which reaches that one interaction. Your Gemini API key and the environment key stay on the poller's machine, and your Prized key never enters a sandbox.
- The script returns once the worker has started and stayed up for a few seconds, so the poller can lease the next interaction. A detached copy of the script stays attached to the worker until it exits, which keeps the sandbox awake for the whole interaction.
- Nobody pauses the sandbox: it pauses itself 2 minutes after its last interaction ends, keeping its files and processes. Two interactions of one environment run in the same sandbox at the same time, each with its own worker, and neither's end freezes the other.
- Nothing connects in: the poller and the worker call out to `generativelanguage.googleapis.com` over HTTPS.

To give an environment another template, set it when you create the environment: `-d '{"mode": "self_hosted", "custom_config": {"prized": {"template": "prized-agent-v1"}}}'`. With `GEMINI_API_KEY` in the poller's terminal instead of an environment key, one poller serves every self-hosted environment in your Gemini project.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `AUTO_PAUSE` | `2m` | How long an environment's sandbox stays up after its last interaction ends |
| `KEEP_PAUSED` | `1d` | How long it waits, paused, for the environment's next interaction before it is deleted (up to `28d`) |
| `RUN_TIMEOUT` | `2h` | The longest one interaction's worker may run before it is stopped (`90m`, `600s`) |
| `MAX_RUNTIME` | Prized's default | Each sandbox's run time, which restarts on every resume: `24h` unless set, up to `7d` on a paid plan; 60 minutes in all on the free tier |
| `PRIZED_TEMPLATE` | `prized-base-v1` | The template of new sandboxes, unless the environment's `custom_config` names one |
| `GEMINI_API_VERSION`, `GEMINI_API_SHA256` | `v0.4.6` and its sha256 | The `gemini-api` release installed in the sandbox. Set both to use another: the sha256 of `gemini-api_Linux_x86_64.tar.gz` is in the release's `checksums.txt` |
| `START_TIMEOUT`, `SETTLE` | `45`, `5` | Seconds the worker gets to start, and seconds it must then stay up, before the script hands off. A worker that exits in that time fails the try. The poller stops the script at its own `--provision-timeout` (2 minutes) |
| `GEMINI_PRIZED_STATE` | `~/.gemini-managed-agents-prized` | Where the poller's machine keeps a log per interaction |

## Limits

- **Working directory** `/home/prized/work`. Tools run as the user `prized`, without root, so there is no `/workspace`.
- **Hosted-only options.** Google refuses `sources`, `network`, `env` and `/files` snapshots on a self-hosted environment (`400 INVALID_ARGUMENT`). Put files in the sandbox yourself, or have the agent fetch them.
- **Network.** A sandbox has outbound internet, with no allow or deny lists yet. Google's guide recommends egress controls for self-hosted workers; if you need them, this is not the setup for that workload.
- **The lease token** is on the worker's command line inside the sandbox, so the agent's own commands can read it. A token reaches only its own interaction, but interactions that run in one sandbox at the same time can read each other's. Use one environment per trust boundary.
- **The handoff is a judgement.** `gemini-api` logs a line when its worker starts on a lease and nothing when its connection to Google opens, so the script hands off when the worker has started and stayed up for `SETTLE` seconds. A worker that fails later than that is not retried by the script; its log says why.
- **Files** last while the environment's sandbox does. If it is deleted (after `KEEP_PAUSED`, at the end of its run time, or if its machine fails), the next interaction starts from an empty working directory.
- **Paused sandboxes keep their disk and saved memory** for `KEEP_PAUSED`; the disk is 10 GB on `prized-base-v1`. On the free tier, 100 GB of disk across the workspace's sandboxes; on a paid plan, paused storage (disk and saved memory) past 20 GB costs $0.05 per GB-month ([Pause and resume](https://prized.dev/docs/sandboxes?via=gemini-managed-agents#pause-and-resume)).
- **Run time.** A sandbox is deleted after 24 hours of running unless you set `MAX_RUNTIME` (up to `7d` on a paid plan); the clock restarts on each resume. On the free tier, 60 minutes of running per sandbox in all, and the idle minutes before each pause count toward it ([Limits](https://prized.dev/docs/limits?via=gemini-managed-agents#sandboxes)).
- **If the poller's machine goes away** mid-interaction, the sandbox pauses 2 minutes later with the worker frozen in it, and that interaction stops making progress.
- **Linux x86_64 sandboxes only**: that is the `gemini-api` build the script installs.

## Troubleshooting

- **An interaction stays queued.** No poller is running for its environment.
- **An environment has two sandboxes.** Its poller ran on two machines. The lock that keeps an environment to one sandbox is a folder on the poller's machine, so run each environment's poller on one machine.
- **The poller prints `ProvisionAndDispatch failed`** with the script's last lines, then tries again after a pause. A `prized:` line means Prized refused the sandbox, for example at the free tier's limits; the line after it says what to change.
- **A resume is refused** for a reason that may pass (a quota, the balance, no host that fits its shape right now), so the try fails after 3 attempts and the sandbox is kept, files and all. Only when the sandbox can't come back (its run time is used up, or it failed or is gone) does the environment get a new one.
- **Logs.** `~/.gemini-managed-agents-prized/logs/<interaction id>.log` says what the script did, and `<interaction id>.worker.log` beside it is the worker's own log.

## Test

```bash
test/test.sh
```

It runs `provision.sh` the way the poller runs it (the lease on stdin, a process group of its own, killed at the timeout, tried again after a failure), against a stand-in for Google's side and a fake `prized`, with no network, no keys and no real sandboxes. The stand-in for the poller is written from the `gemini-api` source (v0.4.6), not from a run against it. The test checks that two interactions of one environment share a sandbox and the second finds the first one's files; that two arriving together make one sandbox; that the script returns while the worker runs and something stays attached until it ends; that only the lease token enters the sandbox and never a command line or a log on the poller's machine; that a failed download and a binary with the wrong sha256 are refused and recovered from; that a refused resume is retried and then fails, while `max_runtime` gets a new sandbox; that a worker Google refuses, one that dies once connected and one that never starts each fail the try; that a try the poller killed leaves no second worker behind, and its lock is taken over; and that malformed leases are refused before any sandbox is touched. The test itself needs Linux (GNU `tar`, `sha256sum`, `setsid`), `python3` and `jq`.

Prized Sandboxes are in beta and Gemini Managed Agents are in preview. Apache-2.0.
