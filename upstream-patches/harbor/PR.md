# feat(environments): add Prized sandbox provider

## What this adds

`-e prized`: each trial runs in its own [Prized sandbox](https://prized.dev/docs/sandboxes), a
Firecracker microVM created over an API.

- `src/harbor/environments/prized/environment.py`: `PrizedEnvironment`, with typed options
  (`template`, `max_runtime_min`, `paused_ttl_min`, `min_vcpu`, `image_headroom_gb`, `labels`,
  `allow_unenforced_users`). `harbor environment schema prized` works without the SDK installed
  (checked with the `prized` import made to fail).
- `src/harbor/environments/prized/_runtime.py`: a stdlib-only script uploaded once per trial. A
  Prized sandbox starts from a fixed template with no Docker and no root, so the script pulls the
  task's prebuilt `docker_image`, unpacks it, and runs each command inside it as the image's root
  with the image's ENV and WORKDIR (a user namespace, a chroot, and a seccomp filter for the
  ownership calls).
- Wiring: `EnvironmentType.PRIZED`, the factory entry, the `harbor[prized]` extra
  (`prized>=0.1.3,<0.2`, also in `cloud`), `uv.lock` (adds `prized` only), `AGENTS.md`, and the
  provider list and capability rows in the docs.

## What it runs, and what it refuses

- Runs: tasks with a prebuilt `docker_image`. CPU and memory requests are honored; fractional CPUs
  are rounded up to whole vCPUs.
- Refused before any sandbox starts: a task with no prebuilt `docker_image` (there is no Dockerfile
  build; with both present the image is used and `force_build` only warns), and anything that needs
  a capability this provider does not declare: Compose, network policies, GPUs, Windows, mounts.
- Refused unless `--ek allow_unenforced_users=true`: a non-root `[agent]` or `[verifier]` user, or an
  image `USER`. Every user is one id underneath, so an agent user could write the grader's files;
  the default is to refuse rather than run the task with a separation it does not have. This check
  runs at the first command for that user, after the sandbox exists.
- A sandbox a crashed run leaves behind pauses after 30 idle minutes and is deleted a day later.

## How it was tested

Offline, on `main` at 5d5e58a2 (Nov 10), in a clean Linux VM with CI's install
(`uv sync --locked --all-packages --all-extras`):

- `ruff check .`, `ruff format --check .` and `ty check` are clean.
- The 92 new unit tests pass (`tests/unit/environments/test_prized.py`, `test_prized_runtime.py`).
  They fake the Prized SDK; bash, tar and unshare are real.
- The registry-wide tests pass (110: `test_options`, `test_optional_imports`,
  `test_provider_resource_capabilities`).
- Full suite (`pytest tests/ packages/rewardkit/tests/unit/ -m "not runtime" -n 4`): 17,837 passed.
  7 tests fail and 15 error, and they are the same 22 that fail on unpatched `main` in that VM
  (17,744 passed there).

Live, against Prized, on `main` at 9c7a39b (Nov 9). Not re-run on today's `main`; of the 8 commits
since, one touches an environment (Blaxel) and none changes the environment interface.
Terminal-Bench 2.1, oracle agent, `-n 6`:

- One full run: 74 of 89.
- 7 of the misses were infrastructure or a flaky task: 5 lost to an archive.ubuntu.com outage (apt
  could not connect; it is in each trial's agent or verifier log), 1 to Docker Hub's anonymous pull limit, and
  `crack-7z-hash`. All 7 passed on a rerun: **81 of 89 with reruns**.
- The 8 that never passed: `build-cython-ext`, `mcmc-sampling-stan`, `protein-assembly`,
  `qemu-alpine-ssh` and `qemu-startup`, which the Oct 6 comment on #3475 reports failing on Modal
  too; and
  `configure-git-webserver`, `git-multibranch` and `mailman`, which need a real user switch.
- The full run created 89 sandboxes and deleted 89.

## Limits

- No Docker in this mode, so Dockerfile and Compose tasks are refused. A Docker-based mode is a
  follow-up, not part of this PR.
- Each trial pulls its image anonymously from inside its sandbox. Docker Hub's anonymous limit cost
  7 of 89 tasks in an earlier full run the same day and 1 in the run above.
- No real user separation (see above), no network policy, no GPUs.

## Demand

CONTRIBUTING asks that at least one user has asked for the integration. As of Nov 10 no user has
asked for this one in an issue or a PR here.

## Agents used

The code and the tests were written by Claude (Anthropic) coding agents run by Prized, and reviewed
by a second agent that did not write them. The 22 baseline failures, the 81 of 89 and the list of
misses were checked against the run logs.

## To run it

```bash
pip install 'harbor[prized]'
export PRIZED_API_KEY=...        # a key with Sandboxes: Write, from prized.dev
harbor run -d terminal-bench/terminal-bench-2-1 -a oracle -e prized -n 6
```

`--ek min_vcpu=4` gives every trial at least 4 vCPUs; `--ek labels=run=<name>` labels a run's
sandboxes so they can be listed and deleted together.
