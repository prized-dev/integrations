# piloop on Prized

A [piloop](https://github.com/hiloopai/piloop) provisioner that runs a pool's workers on [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=piloop): Firecracker microVMs that pause with their memory and fork into live copies. The provider sets up one sandbox once (piloop and your `setup`), pauses it, and starts each worker as a fork of it, so no worker repeats the install.

## Run it

1. Make a Prized API key with **Sandboxes: Write** at https://prized.dev/dashboard/workspace/api-keys?via=piloop, and set it where the piloop daemon runs:

   ```bash
   export PRIZED_API_KEY=dcp_…
   ```

2. Install the provider beside piloop (it is not on PyPI yet):

   ```bash
   uv tool install --reinstall piloop --with 'piloop-prized @ git+https://github.com/prized-dev/integrations#subdirectory=piloop'
   ```

3. Point a pool's `compute` table at it in `$PILOOP_HOME/config.toml`, then `piloop restart`:

   ```toml
   [plugins.executor.pools.agents.provisioner]
   max = 4

   [plugins.executor.pools.agents.provisioner.compute]
   provider = "prized"
   cpu = 2
   memory_mb = 8192
   setup = ["npm install -g @openai/codex@0.166.0 @anthropic-ai/claude-code@2.1.310"]
   ```

   `piloop doctor` checks the SDK and the key.

A table written for `modal` keeps `cpu`, `memory_mb`, `memory_limit_mb`, `timeout_hours`, `start_seconds`, `call_seconds` and `env`: change `provider`, and move what the image installed into `setup`.

## What happens

- **First start:** the provider creates a sandbox from the template, installs uv and piloop (the release that runs the daemon, or the git commit it was installed from), runs your `setup` lines, and pauses it: the **warm parent**.
- **Each worker:** a fork of the parent, labelled `piloop-owner`, `piloop-name` and `piloop-pool`. The provider writes the worker's credential to a file in it (mode 0600) and starts `piloop worker` with a command that reads the file and deletes it. The credential is never in a command line, a label or the sandbox's environment. The worker logs to `~/.piloop-prized/worker.log`.
- **Stop:** the provider deletes by the worker's labels. A sandbox whose worker did not start is deleted at once.
- **More workers than one machine holds:** a parent's copies start on its machine. When it is full, the provider sets up another parent, up to `max_parents`.
- **A restarted daemon** finds its paused parent by its labels and forks it again.
- **Cleanup:** `python -m piloop_prized cleanup --home $PILOOP_HOME` deletes every sandbox of that server, parents too. A paused parent also deletes itself `parent_hours` after its pause.

With `fork = false`, each worker is a fresh sandbox that runs the setup itself before its worker starts.

A setup that runs longer than the pool's `register_seconds` (600 by default): set up the parent ahead, and name it.

```bash
python -m piloop_prized parent compute.toml   # the compute table's keys; prints sbx_…
```

```toml
[plugins.executor.pools.agents.provisioner.compute]
provider = "prized"
parent = "sbx_…"
```

## Keys

| Key | Default | What it does |
| --- | --- | --- |
| `template` | `prized-agent-v1` | `prized-base-v1` (Ubuntu, Python 3.12, Node 22, git) or `prized-agent-v1` (that, with Claude Code and Codex) |
| `cpu`, `memory_mb` | the template's | Each sandbox's vCPUs and MiB of memory, up to 32 vCPU and 128 GB. The memory is the microVM's whole memory, so a hard limit |
| `memory_limit_mb` | | Taken for a table written for Modal: the microVM gets this much |
| `setup` | `[]` | Shell lines run once in the parent after piloop is installed: one bash script, `set -euo pipefail`, from `/home/prized/work` |
| `piloop` | the daemon's own | What `uv tool install` installs as piloop, such as `piloop==0.5.0` or a `git+https://…@<sha>` |
| `fork` | `true` | `false`: each worker is a fresh sandbox that runs the setup itself |
| `parent` | | A paused sandbox that holds the setup already, from `python -m piloop_prized parent` |
| `max_parents` | `4` | The most warm parents the provider sets up |
| `timeout_hours` | `24` | The longest a sandbox runs, up to 168; each fork gets the whole of it |
| `parent_hours` | `24` | How long a paused parent waits before Prized deletes it; `0` keeps it until cleanup |
| `setup_seconds` | `3600` | The longest the setup may run |
| `start_seconds`, `call_seconds` | `600`, `60` | The bounds of a start, and of a list or a stop |
| `env` | `{}` | More environment of each worker: never a secret |
| `secret_env` | `[]` | Variables of the daemon's environment that each worker gets in its credential file, such as a CAS's keys |
| `region`, `cloud` | | Taken when they say `us-west-2` and `aws`: anything else is refused |

## Limits

- **No custom images yet.** A sandbox starts from a Prized template, as the user `prized` without sudo: put what an image would add in `setup`, under `/home/prized` (`pip` and `npm install -g` install to `~/.local`). [Use a sandbox as your own template](https://prized.dev/docs/sandboxes-own-template?via=piloop) shows how to add system packages without root. `image`, `image_id` and `registry` are refused with that advice.
- **No GPUs.** `gpu` is refused: keep a GPU pool on Modal.
- **No egress allowlist.** Every sandbox reaches the internet, so `outbound_domains` is refused.
- **One region.** Sandboxes run in us-west-2 (Oregon).
- **No named secrets.** `secrets` is refused: name the daemon's variables in `secret_env`.
- A sandbox runs as long as `timeout_hours`, then Prized deletes it, and piloop starts another for the jobs that wait.

## Tests

```bash
scripts/test.sh piloop    # lint, types and offline tests against fakes of the Prized SDK
```

The offline tests run piloop's own provisioner conformance check against the provider, as piloop's tests do for `modal` and `daytona`. The live test creates real sandboxes, billed to your workspace, and deletes them:

```bash
PRIZED_LIVE=1 PRIZED_API_KEY=dcp_… pytest tests/test_live.py -rs
```

## License

[Apache-2.0](LICENSE)
