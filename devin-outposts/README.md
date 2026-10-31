# Devin Outposts on Prized

Run each [Devin Outposts](https://docs.devin.ai/cloud/outposts/overview) session in a [Prized sandbox](https://prized.dev/docs/sandboxes?via=devin-outposts) of its own. Devin plans in Cognition's cloud; the sandbox, a Firecracker microVM, runs its commands, edits and git. When the session sleeps, the sandbox pauses with its memory, so the dev servers and databases it left running are still there when it wakes, about a second later.

## Run it

1. In Devin, create an outpost for Linux (**Settings → Environment → Outposts → Create Outpost**) and copy its ID (`outpost_env-…`). Under **Settings → Devin API → Service users**, make a service user whose role has **Outposts write**, and copy its token (`cog_…`).

2. Make a Prized API key with **Sandboxes: Write** at https://prized.dev/dashboard/workspace/api-keys?via=devin-outposts. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=devin-outposts. It can also install the Prized skill with `npx skills add prized-dev/skills -g -y` (it should ask you first), so it knows when and how to use Prized the next time you ask for a sandbox.

3. On a machine that stays on (a laptop, a VM, a container; it needs only outbound HTTPS), install the orchestrator and export the three values:

   ```bash
   pip install "git+https://github.com/prized-dev/integrations#subdirectory=devin-outposts"
   export DEVIN_OUTPOSTS_TOKEN=cog_…
   export OUTPOST_ID=outpost_env-…
   export PRIZED_API_KEY=dcp_…
   ```

4. Start it:

   ```bash
   prized-devin-outposts run
   ```

5. In Devin, start a session and pick your outpost under **Configuration → Virtual environment**. `prized sandbox ls` shows its sandbox, labelled `devin-session=devin-…`.

## What happens

- The orchestrator watches the outpost's queue and claims a waiting session. It creates a sandbox for it (from `prized-base-v1`, 4 vCPU and 8 GB, unless you choose otherwise), downloads the `devin-remote` build Devin pinned for the session, checks it against its published sha256, and starts it from `/home/prized/work`. Devin's repositories go in `/home/prized/work/repos`.
- The remote connects out to Devin's gateway with a connect token for that one session. That is the only Devin credential in the sandbox; the service-user token and your Prized key stay on the orchestrator's machine.
- **Session sleeps:** the remote exits, the orchestrator pauses the sandbox (memory, processes and disk kept; no compute billed) and returns the session to the queue.
- **Session wakes:** the orchestrator claims it again, resumes the same sandbox (about 0.6 s), and starts a new remote with the new token. Files, branches, installed packages, and anything still running are where Devin left them.
- **Session ends:** the sandbox is deleted. A session that leaves the queue without Devin saying it ended has its sandbox paused instead, and deleted after `KEEP_PAUSED`.
- The orchestrator renews its claims before they expire, starts a crashed remote again (up to three launches in all, then it pauses the sandbox and tells the session), and deletes the sandboxes of ended sessions. A restarted orchestrator finds its sandboxes by their labels and carries on; two orchestrators on one outpost never serve the same session.

## Start new sessions warm

Set up a sandbox once (repositories cloned, dependencies installed, services running) and every new session starts as a live copy of it, in about 2 seconds:

```bash
prized-devin-outposts warm --repo https://github.com/you/app --setup "cd repos/app && npm ci"
export WARM_PARENT=sbx_…   # the id it prints
prized-devin-outposts run
```

The parent stays paused. Each session forks it, memory included, so a dev server started in `--setup` is already running in the session. Forked sessions keep the parent's `AUTO_PAUSE` and `KEEP_PAUSED`, so set those when you run `warm`. For private repositories, clone with a token in a credential helper you set up in `--setup`, never in the URL.

## Connect from Devin's consent page

Instead of step 1, a Devin admin can connect an outpost from their browser:

```bash
prized-devin-outposts connect
prized-devin-outposts run --env-file ~/.config/prized-devin-outposts/outpost.env
```

`connect` opens Devin's connection page; the admin confirms, Devin creates the outpost and a service user, and the token comes back to this machine without passing through the browser. **Not available yet:** Devin accepts the connection only once Cognition lists Prized's callback (`http://localhost:8977/prized/devin/callback`). Until then, use the steps above.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `MAX_CONCURRENT_SESSIONS` | `4` | Sessions this orchestrator serves at once |
| `PRIZED_TEMPLATE` | `prized-base-v1` | The sandbox template ([your own](https://prized.dev/docs/sandboxes-own-template?via=devin-outposts) can add Chrome, ffmpeg and your toolchains) |
| `PRIZED_VCPU`, `PRIZED_MEMORY_GB`, `PRIZED_DISK_GB` | The template's (4 vCPU, 8 GB, 10 GB) | Each session's shape, up to 32 vCPU and 128 GB |
| `KEEP_PAUSED` | `7d` | How long a sleeping session's sandbox waits for a wake before it is deleted (up to `28d`; `0` keeps it until you delete it) |
| `MAX_RUNTIME` | Prized's default, `24h` | How long a sandbox may run before it is deleted; the clock restarts on every wake (up to `7d` on a paid plan) |
| `AUTO_PAUSE` | `15m` | If the orchestrator stops, its sessions' sandboxes pause after this long without it |
| `WARM_PARENT` | | A paused sandbox to fork new sessions from |
| `PRIZED_LABELS` | | Extra labels on every sandbox, `team=infra,cost=devin` |
| `DEVIN_CHROME_PATH` | | A Chrome binary in your template, for Devin's browser |
| `IGNORE_NETWORK_POLICY` | | `1` serves sessions whose network policy Prized can't enforce (see Limits) |
| `ACCEPTOR_ID`, `STATE_DIR` | generated, `~/.local/state/prized-devin-outposts` | This orchestrator's identity in the queue; never share one between machines |

## Limits

- **Network policy.** Sandboxes have open outbound internet and no allow lists yet, so a session whose [security profile](https://docs.devin.ai/product-guides/security-profiles) restricts its network is refused with a message in the session, unless you set `IGNORE_NETWORK_POLICY=1`.
- **No root.** Commands run as the user `prized` without sudo, so Devin can't `apt install`. Put system packages in [your own template](https://prized.dev/docs/sandboxes-own-template?via=devin-outposts).
- **Browser and recordings** need Chrome and ffmpeg in the template, which `prized-base-v1` doesn't have.
- **Free tier:** sandboxes there are capped at 2 vCPU and 4 GB (set `PRIZED_VCPU=2 PRIZED_MEMORY_GB=4`) and 60 minutes of running each, in all. A top-up or a paid plan lifts both.
- **Linux only**, on x86-64, in us-west-2. Prebuilt session images (`spec.image`) and image-build sessions are not used; sessions set themselves up in the sandbox.
- **What survives a sleep:** the remote itself exits when Devin puts a session to sleep, so the shells it owned end with it. Processes started in the background, files and caches are kept.

## Use the core for other agent pools

`prized-machines` is the same session-to-sandbox core without Devin: `ensure` (resume the key's paused sandbox, fork the warm parent, or create one), `hibernate` (pause with memory) and `destroy`. A spawn hook for another pool, such as Cursor's self-hosted workers, can call it:

```bash
prized-machines ensure --key cursor-worker=w-123 --label cursor-pool=prized
prized-machines hibernate --key cursor-worker=w-123
```

## Tests

```bash
scripts/test.sh devin-outposts
```

Offline, with no keys: a stand-in for Devin's Outposts service (queue, claims and renewals, watch, gateway, binary host, token endpoint) that follows Cognition's own mock of its API, fake sandboxes that run the real in-sandbox scripts with bash and freeze processes on pause, and a fake `devin-remote`. They cover the whole lifecycle, restart recovery, two orchestrators racing, claim renewal, a crashed worker, a binary that fails its checksum, refused network policies, warm forks and the janitor.

Live, against real sandboxes (billed to your workspace, deleted at the end):

```bash
PRIZED_LIVE=1 PRIZED_API_KEY=dcp_… pytest tests/test_live.py
```
