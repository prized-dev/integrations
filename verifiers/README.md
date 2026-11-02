# verifiers-prized

Run [verifiers](https://github.com/PrimeIntellect-ai/verifiers) rollouts in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=verifiers) (Firecracker microVMs). The environment is built once, in a parent sandbox that is then paused; each rollout gets a fork of it, with the parent's disk, memory and running processes, so it starts warm. No verifiers fork or PR needed: it is a verifiers runtime plugin (`type = "custom"`).

```bash
pip install "verifiers-prized @ git+https://github.com/prized-dev/integrations#subdirectory=verifiers"   # not on PyPI yet
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=verifiers
```

Then point the agent's runtime at it in your eval or training config:

```toml
[env.agent.runtime]
type = "custom"
import_path = "verifiers_prized.PrizedBackend"
setup = [
  "curl -LsSf https://astral.sh/uv/install.sh | sh",
  "python3 -m venv ~/.venv && ~/.venv/bin/pip install -q numpy",
]
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=verifiers.

## How it works

1. The first rollout of a process creates a sandbox, runs `setup` in `/home/prized/work`, and pauses it. A paused sandbox bills for storage only.
2. Each rollout forks the parent and gets its own running copy. Rollouts that start together are forked in one request.
3. When the rollout ends, its copy is deleted. The parent lives until the process exits, then it is deleted too.

A process started in `setup` with `nohup ... &` (a server, a loaded model, a warmed cache) keeps running in every copy. Task hooks still run per rollout, in the copy.

Measured on 11-02-2026 with prized 0.1.2, 1 vCPU and 2 GB: the parent was running 3.7 s after the create, and ready (4 setup commands, paused) at 4.7 s. One fork request then had 4 copies running in about 2.5 s. In each copy a server started during setup answered with its in-memory token, without a restart. A short `run` took 0.11 s.

## Settings

Every field goes next to `type` and `import_path`.

| Field | Default | What it does |
| --- | --- | --- |
| `setup` | `[]` | Shell commands that build the environment, run once in order in the parent, without root |
| `setup_env` | `[]` | Names of variables from your environment passed to `setup` only (the names are saved with the config, not the values) |
| `setup_timeout` | `1800` | Seconds each setup command may take; `0` for no limit |
| `template` | `prized-base-v1` | `prized-base-v1` (Ubuntu 24.04, Python 3, Node.js 22, git, build tools) or `prized-agent-v1` (adds Claude Code and Codex) |
| `cpu`, `memory`, `disk` | `1`, `2`, `10` | vCPUs, GB, GB. A task's `resources` apply when these are left at their defaults; each shape gets its own parent |
| `workdir` | task's, else `/home/prized/work` | Where commands run. Must be under `/home/prized` or `/tmp` |
| `parent` | none | Fork this sandbox instead of building one (below). It is never paused, resumed or deleted here |
| `fork` | `true` | `false` gives every rollout a new sandbox that runs `setup` itself, with nothing shared |
| `pause_parent` | `true` | Pause the parent after `setup` |
| `max_parents` | `4` | Parents per shape and process when machines fill up (below) |
| `capacity_wait` | `600` | Seconds a rollout waits for room before it fails |
| `max_runtime_min` | Prized's (24 h) | Run time of each sandbox; copies get their parent's |
| `blocking_run_timeout` | none | Send each `run` as one blocking call killed after this many seconds (up to 240): 0.02 s instead of 0.11 s per short command, wrong for long ones |
| `labels` | `{}` | Labels on every sandbox it makes |
| `api_key_env` | `PRIZED_API_KEY` | The variable holding the API key |

### Reuse one parent across runs

```bash
python -m verifiers_prized build --setup 'python3 -m venv ~/.venv' --setup '~/.venv/bin/pip install -q numpy'
```

prints the id of a paused sandbox with the environment built. Set `parent = "<id>"` and every run forks it with no setup. It is yours to delete: `prized sandbox rm <id>`.

### Many rollouts at once

Copies start on their parent's machine, which holds about 31 copies of a 1 vCPU sandbox (15 of 2 vCPU, 7 of 4). Past that a fork answers `409 fork_capacity`: the runtime forks the number that fits, rests that parent briefly, and once every parent is full builds another one, up to `max_parents`. Workspace quotas and rate limits are waited out for up to `capacity_wait`. Each process (each verifiers env-server worker) builds its own parents.

## Limits

- **Oregon only.** Sandboxes run in `us-west-2`.
- **No root, no Docker.** Commands run as the user `prized` without sudo. Install into your home: a venv, `pip --user`, `uv`, `npm` with a user prefix, or apt packages unpacked into the home ([how](https://prized.dev/docs/sandboxes-own-template?via=verifiers#2-add-system-packages-without-root)).
- **Environments are built by `setup` and forks, not images.** There are no custom images yet, so tasks that set `image`, need a container-only harness or isolated grading, set `agent_user`, run Compose services, put a tool server in its own runtime, or set a network policy are refused by verifiers before anything is created. Prime and Docker runtimes run those.
- **The model call goes through a tunnel.** A harness that runs the agent inside the sandbox reaches verifiers' interception server on your machine through `interception.tunnel`: Prime's by default (needs `prime login`), or your own endpoint (`type = "custom"`, `url`, `port`).
- **Streams are text.** `exec` output arrives through the SDK as UTF-8, so bytes that are not valid UTF-8 become U+FFFD; `read` is byte-exact. Each `exec` stdin write is one request.
- **Copies share the parent's memory**, random state included: reseed or restart a process after a fork before it makes keys or IDs.
- **No GPUs.** A task's `resources.gpu` is ignored with a warning.
- **Free tier:** 10 sandboxes at once, each up to 2 vCPU, 4 GB and 60 minutes, so a parent and 9 copies.

Every sandbox carries the label `vfp-run=<id>`, logged when the parent is built. Each copy is deleted when its rollout ends, and at exit (including Ctrl-C) the rest are deleted by that label. After a `kill -9`, delete them yourself: `prized sandbox ls` lists them.

## The verifiers contract

`PrizedBackend` subclasses verifiers' `RuntimeBackend` (verifiers 0.4.1.dev132 and later; 0.4.0 has no runtime plugins) and sets `config_class = PrizedConfig`. verifiers constructs it as `PrizedBackend(config, name)` once per rollout, calls `start()`, then reaches it through `Runtime` views, and `stop()` runs `teardown()` shielded from cancellation.

| Method | Here |
| --- | --- |
| `start()` | A copy from the parent pool, or a new sandbox with `fork = false` |
| `teardown()` / `cleanup()` | Delete the copy (async; sync at interpreter exit) |
| `run(runtime, argv, env)` | One SDK command, detached and polled (no time limit), stdin closed |
| `exec(runtime, argv, env)` | A background command whose output streams; stdin through a FIFO |
| `run_background(runtime, argv, env, log)` | A detached background command writing to `log` |
| `write` / `read` | The files API under `/home/prized`; other paths through a command |
| `expose`, `prepare_execution`, `host_url` | Not supported, none, unchanged |

`SUPPORTS_TASK_IMAGES`, `SUPPORTS_USER_SWITCHING`, `SUPPORTS_PORT_EXPOSURE` and `SUPPORTS_COMPOSE` are false, the config does not inherit `NetworkPolicyConfig`, and `is_local` is false.

## Tests

```bash
scripts/test.sh verifiers          # from the repo root: ruff, mypy, then the offline tests
FLOOR=1 scripts/test.sh verifiers  # the oldest supported verifiers and prized
```

The tests run against two stand-ins: `tests/fake_verifiers` in place of verifiers, and `tests/fake_prized.py` in place of the Prized API (sandboxes as directories, commands run by the local `sh`, forks as copies). `tests/test_contract.py` holds them to the installed verifiers source (parsed, not imported) and the SDK's signatures, and mypy checks the backend against verifiers' own types.

License: Apache-2.0.
