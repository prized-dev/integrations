# verifiers-prized

Run [verifiers](https://github.com/PrimeIntellect-ai/verifiers) rollouts in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=verifiers) (Firecracker microVMs). Your environment is built once, in a parent sandbox that is then paused. Each rollout gets a fork of it: its own microVM with the parent's disk, memory and running processes. It is a verifiers runtime plugin (`type = "custom"`), so there is no verifiers fork or PR.

## Install

```bash
pip install "verifiers-prized @ git+https://github.com/prized-dev/integrations#subdirectory=verifiers"   # not on PyPI yet
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=verifiers
```

It needs verifiers 0.4.1.dev132 or later (0.4.0 has no runtime plugins) and Python 3.11+. Install it wherever the config is loaded. No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=verifiers.

## Use

Point the agent's runtime at it in your eval or training config, and put what the environment needs in `setup`:

```toml
[env.agent.runtime]
type = "custom"
import_path = "verifiers_prized.PrizedBackend"
setup = [
  "curl -LsSf https://astral.sh/uv/install.sh | sh",
  "python3 -m venv ~/.venv && ~/.venv/bin/pip install -q numpy",
]
```

For `vf-validate`, the same fields go under `[runtime]`. Every field is in [Settings](#settings).

## How it works

1. The first rollout in a process creates a sandbox, runs `setup` in `/home/prized/work` and pauses it. A paused sandbox bills for storage only.
2. Each rollout forks that parent and gets its own running copy. Rollouts that start together are forked in one request.
3. A copy is deleted when its rollout ends. The parent is deleted when the process exits.

Whatever `setup` leaves behind is in every copy: files, installed packages, and processes started in the background (a server, a loaded model, a warmed cache). Send such a process's output to a file, `nohup server > server.log 2>&1 &`: a setup command's own output pipes close once it ends. Task and harness hooks still run in each copy, per rollout.

## Measured

One run on 2026-11-03 with verifiers 0.4.1.dev147, prized 0.1.2, and 1 vCPU / 2 GB sandboxes in Oregon. It used verifiers' own `vf-validate --only-gold` on verifiers' `gsm8k` environment, 8 tasks at a time (`-c 8`). In each task, `setup` builds the uv environment for gsm8k's grader (`verify.py`, which needs `math-verify`) inside the sandbox, then `validate` grades the reference answer with it there. All 64 tasks graded valid.

Cold (`fork = false`, so every task's sandbox runs `setup`) and forked (`setup` runs once, in the parent) ran the same `setup` list. The light list installs uv and builds the grader's environment; the heavy list adds a venv with CPU PyTorch. Times run from verifiers asking for a sandbox to the grade; a task's parts are medians.

| | Light setup | Heavy setup (adds PyTorch) |
| --- | --- | --- |
| Cold: a batch of 8 | 9.5 s | 27.7 s |
| Cold: one task (create, `setup`, grade) | 7.5 s (3.8 + 2.0 + 1.6) | 27.2 s (25.5 + 0.2 + 1.6) |
| Forked: build the parent once (create, `setup`, pause) | 5.5 s | 20.3 s |
| Forked: first batch of 8, parent build included | 12.0 s | 30.4 s |
| Forked: each later batch of 8 from the same parent | 6.7 s, 6.8 s | 9.8 s |
| Forked: one task in a later batch (to a copy, `setup`, grade) | 5.5 s and 5.8 s (2.2 to 2.6 + 0.6 + 2.7) | 9.2 s (3.5 + 1.0 + 4.6) |
| Sandbox-seconds per task, from Prized's records | cold 8.4, forked 6.6 plus the parent | cold 28.4, forked 10.1 plus the parent |

How to read it:

- From nothing, the first forked batch is slower than a cold one, because the parent is built first.
- The gain is in each later batch from the same parent: 1.4x faster with the light setup and 2.8x with the heavy one in this run. It grows with how long `setup` takes.
- A copy read what its parent had built more slowly than a sandbox reads what it built itself: grading took 2.7 s in a copy against 1.6 s cold with the light setup, and 4.5 s against 1.6 s with the heavy one. In copies of a parent with no `setup`, which built the grader themselves, it took 1.6 s.
- The parent ran for as long as its build took (5.5 s and 20.3 s), then sat paused until the process exited.

On 2026-11-02 a server started with `nohup` in `setup` answered from each of 4 copies with the token it held in memory, without a restart.

## Settings

Every field goes next to `type` and `import_path`.

| Field | Default | What it does |
| --- | --- | --- |
| `setup` | `[]` | Shell commands that build the environment, run once in order in the parent, as the user `prized` without root. One that fails fails the process's rollouts without being run again, so put retries for flaky downloads in the command itself |
| `setup_env` | `[]` | Names of variables from your environment passed to `setup` (the names are saved with the config, not the values). A process `setup` leaves running keeps them, in every copy, where the agent can read them. Never the Prized API key |
| `setup_timeout` | `1800` | Seconds each setup command may take; `0` for no limit (a setup that never ends then holds the rollouts waiting for it, up to verifiers' own `timeouts.setup`) |
| `template` | `prized-base-v1` | `prized-base-v1` (Ubuntu 24.04, Python 3, Node.js 22, git, build tools) or `prized-agent-v1` (adds Claude Code and Codex) |
| `cpu`, `memory`, `disk` | `1`, `2`, `10` | vCPUs, GB, GB. A task's `resources` apply when these are left at their defaults; each shape gets its own parent |
| `workdir` | task's, else `/home/prized/work` | Where commands run. Must be under `/home/prized` or `/tmp` |
| `parent` | none | Fork this sandbox instead of building one ([below](#reuse-one-parent-across-runs)). It is never paused, resumed or deleted here |
| `fork` | `true` | `false` gives every rollout a new sandbox that runs `setup` itself, with nothing shared |
| `pause_parent` | `true` | Pause the parent after `setup` |
| `max_parents` | `4` | Parents per shape and process when machines fill up ([below](#many-rollouts-at-once)) |
| `capacity_wait` | `600` | Seconds a rollout keeps trying once it has been refused room (a full machine, a quota); `0` fails it at the first refusal. The time a parent's `setup` takes does not count. With `fork = false` a refused create fails its rollout at once |
| `max_runtime_min` | Prized's (24 h; 60 min on the free tier) | Run time of each sandbox; copies get their parent's |
| `blocking_run_timeout` | none | Send each `run` as one blocking call killed after this many seconds (up to 240): faster for many short commands, wrong for long ones |
| `labels` | `{}` | Labels on every sandbox it makes |
| `api_key_env` | `PRIZED_API_KEY` | The variable holding the API key |

### Reuse one parent across runs

```bash
python -m verifiers_prized build --setup 'python3 -m venv ~/.venv' --setup '~/.venv/bin/pip install -q numpy'
```

prints the id of a paused sandbox with the environment built. Set `parent = "<id>"` and every run forks it with no setup. It is yours to delete: `prized sandbox rm <id>`.

### Many rollouts at once

Copies start on their parent's machine, which holds at most 31 copies of a 1 vCPU sandbox, 15 of 2 vCPU and 7 of 4 vCPU ([fork](https://prized.dev/docs/sandboxes?via=verifiers#fork)). Past that a fork answers `409 fork_capacity`: the runtime forks the number that fits, rests that parent briefly, and once every parent is full builds another one, up to `max_parents`. Workspace quotas and rate limits, on a fork or on a parent's create, are waited out for up to `capacity_wait`. A parent that can no longer fork (its machine retiring, its memory gone) is replaced. If 3 new parents in a row cannot fork before their first copy, the rollouts fail rather than run `setup` again and again. A rollout still waiting its turn behind others has not been refused, so `capacity_wait` does not bound that wait. Each process (each verifiers env-server worker) builds its own parents.

## What works and what doesn't

Tested against real sandboxes on 2026-11-02 and 11-03: `run`, `exec` (stdout and stderr byte for byte, stdin while it runs, terminate and kill), `run_background`, `read` and `write` (under `/home/prized` and `/tmp`), forking a parent built here, `fork = false`, and `python -m verifiers_prized build` with `parent = "<id>"`. verifiers' own runtime tests pass against it (`tests/unit/test_runtimes.py`, [below](#tests)): its 9 runtime cells on 2026-11-03, 4 on live sandboxes and 5 that check a refusal. Tested only offline: task `resources`, a `workdir` of your own, and the capacity and refusal paths.

Not run yet: a model-driven `vf-run`, where a harness inside the sandbox reaches verifiers' interception server through `interception.tunnel` (Prime's by default, which needs `prime login`, or your own endpoint), and a prime-rl training run.

verifiers refuses these before anything is created, because Prized has no custom images yet:

- tasks that set `image`, container-only harnesses, and isolated grading
- `agent_user`, a tool server in its own runtime, Compose services, and network policies

Prime's and Docker's runtimes run those. Also:

- **Oregon only.** Sandboxes run in `us-west-2`.
- **No root, no Docker.** Commands run as `prized` without sudo. Install into your home: a venv, `uv`, `npm` with a user prefix, or apt packages unpacked into the home ([how](https://prized.dev/docs/sandboxes-own-template?via=verifiers#2-add-system-packages-without-root)). Plain `pip install` and `pip --user` stop at `externally-managed-environment`.
- **No GPUs.** A task's `resources.gpu` is ignored, with a warning from verifiers.
- **Copies share the parent's memory**, random state included: reseed or restart a process after a fork before it makes keys or IDs.
- **`exec` streams bytes, `run` returns text.** `exec` output crosses as base64, a line per read, so binary output (ACP's length-prefixed frames, say) arrives byte for byte. `run` returns text, as verifiers' `ProgramResult` does, and fails rather than return output the SDK cut short (about 64 MiB a stream; 100 MiB with `blocking_run_timeout`). `read` is byte-exact. Each `exec` stdin write is a request per 48 KiB, and an `exec` whose command leaves a child running with its stdout reports the exit up to 2 s late. If a process is lost with its sandbox (its run time over, its machine gone), `wait()` raises a `SandboxError` naming the cause and `poll()` gives `-1`.
- **Free tier:** 10 sandboxes at once (a paused parent counts toward the workspace's disk instead), each up to 2 vCPU, 4 GB and 60 minutes of running, and 20 vCPU and 40 GB running across the workspace ([free tier](https://prized.dev/docs/sandboxes?via=verifiers#free-tier)).

## Cleanup

Every sandbox carries the label `vfp-run=<id>`, logged when the parent is built (verifiers' CLIs show it at their usual level). Each copy is deleted when its rollout ends, and at exit (including Ctrl-C) the rest are deleted by that label, which a forked child process does not share. A signal that skips Python's exit hooks (`kill -9`, or a plain SIGTERM to a script of your own, as a scheduler's preemption sends) leaves them running until `max_runtime_min`: delete them yourself (`prized sandbox ls` lists them), or set `max_runtime_min` low.

## The verifiers contract

`PrizedBackend` subclasses verifiers' `RuntimeBackend` and sets `config_class = PrizedConfig`. verifiers constructs it as `PrizedBackend(config, name)` once per rollout, calls `start()`, then reaches it through `Runtime` views, and `stop()` runs `teardown()` shielded from cancellation.

| Method | Here |
| --- | --- |
| `start()` | A copy from the parent pool, or a new sandbox with `fork = false` |
| `teardown()` / `cleanup()` | Delete the copy (async; sync at interpreter exit) |
| `run(runtime, argv, env)` | One SDK command, detached and polled (no time limit), stdin closed |
| `exec(runtime, argv, env)` | A background command whose output streams byte for byte; stdin through a FIFO |
| `run_background(runtime, argv, env, log)` | A detached background command writing to `log` |
| `write` / `read` | The files API under `/home/prized`; other paths through a command |
| `expose`, `prepare_execution`, `host_url` | Not supported, none, unchanged |

`SUPPORTS_TASK_IMAGES`, `SUPPORTS_USER_SWITCHING`, `SUPPORTS_PORT_EXPOSURE` and `SUPPORTS_COMPOSE` are false, the config does not inherit `NetworkPolicyConfig`, and `is_local` is false.

## Tests

```bash
scripts/test.sh verifiers          # from the repo root: ruff, mypy, then the offline tests
FLOOR=1 scripts/test.sh verifiers  # the oldest supported verifiers and prized
```

The offline tests run against two stand-ins: `tests/fake_verifiers` in place of verifiers, and `tests/fake_prized.py` in place of the Prized API (sandboxes as directories, commands run by the local `sh`, forks as copies). `tests/test_contract.py` holds them to the installed verifiers source (parsed, not imported) and the SDK's signatures, and mypy checks the backend against verifiers' own types.

To test against real sandboxes, with your own key in `PRIZED_API_KEY` (billed to your workspace), run verifiers' runtime tests from a verifiers checkout:

```bash
uv run pytest tests/unit/test_runtimes.py --runtime '{"type": "custom", "import_path": "verifiers_prized.PrizedBackend"}'
```

License: Apache-2.0.
