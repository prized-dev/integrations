# harbor-prized

Run [Harbor](https://github.com/harbor-framework/harbor) tasks, such as Terminal-Bench, in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=harbor) (Firecracker microVMs). No Harbor fork or PR needed.

```bash
uv tool install harbor --with "harbor-prized @ git+https://github.com/prized-dev/integrations#subdirectory=harbor"   # harbor-prized is not on PyPI yet
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=harbor
harbor run -d terminal-bench/terminal-bench-2-1 -a oracle -e harbor_prized:PrizedEnvironment -n 4
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=harbor.

Each trial gets its own sandbox, sized from the task's `cpus`, `memory_mb` and `storage_mb`, plus 20 GB of disk for the image, and deleted when the trial ends (`--no-delete` pauses it instead, at storage cost only, and it is deleted after a day paused). If Harbor itself dies mid-run, a sandbox it leaves behind pauses after 30 idle minutes, so it stops billing compute, and is deleted a day later. Paused sandboxes count toward the workspace's disk quota until then. The task's prebuilt `docker_image` is unpacked with `crane`, keeping its file modes, and every command runs chrooted into it with the image's `ENV`, `WORKDIR` and `USER`, the task's env, Docker's umask (022) and a Docker-style `/etc/hosts`. Root commands run as uid 0 of an unprivileged user namespace, under a seccomp filter that makes `chown`, setuid/setgid bits and switches to other users succeed without effect, as fakeroot does, so `apt-get`, `useradd` and `sudo` work. A command for another user (`user=agent`, the task's `[agent] user`, or the image's `USER`) runs as that user's uid and gid in a namespace nested in root's. All commands of a trial share these namespaces, so processes they leave running (tmux, servers) stay visible to later commands. Long commands run detached through the [`prized` SDK](https://pypi.org/project/prized/), so an hour-long agent run outlives a dropped connection, and files over 64 MiB move in parts.

Options (`--ek key=value`): `docker` (`auto`, the default: [Docker mode](#dockerfile-and-compose-tasks) for Dockerfile and Compose tasks, and for tasks with a no-network phase; `true` for every task; `false` never), `template` (default `prized-base-v1`), `docker_template` (`prized-docker-v1`), `dockerd_wait_sec` (120), `max_runtime_min` (1440; up to 10080, 7 days), `paused_ttl_min` (1440, a day), `min_vcpu` (1), `image_headroom_gb` (20), `labels` (`k=v,k=v`), `registry_auth` (`env`, `docker` or a path; [below](#pull-with-your-own-registry-credentials)), `fork_mode` (false; [below](#fork-mode-one-warm-start-per-task)), `fork_disk_budget_gb` (120). Each sandbox is labelled `harbor=1` and `harbor-session=<trial>`, so once no run is going, `prized sandbox rm --label harbor=1` removes any a crashed run left behind. A task's `cpus` may be fractional (`0.5`, Harbor 0.25 and later): the sandbox gets whole vCPUs, rounded up.

## Fork mode: one warm start per task

With `-k 3` or more, each task's image is pulled and unpacked once, and every trial of the task forks that start instead of making its own:

```bash
harbor run -d terminal-bench/terminal-bench-2-1 -a oracle -e harbor_prized:PrizedEnvironment \
  --ek fork_mode=true --plugin harbor_prized:TaskMajor -k 3 -n 4
```

- The first trial of a task builds a parent: the usual start, then paused. Each trial, that one included, runs in its own fork of it, from the same state, and the fork is deleted when the trial ends. No trial runs in the parent, and the pull credentials are gone from it before it pauses.
- A fork takes about 1.4 s (the first fork of a parent with 8 GB of memory takes 10 to 25 s more today). A fresh Terminal-Bench 2.1 start took 11 s on average (median 10 s). Docker Hub sees one pull per task instead of one per trial.
- Paused parents count toward the workspace's disk quota, running trials too. `fork_disk_budget_gb` (120) caps the disk the parents hold. A parent is built only if a later trial of its task is planned (from Harbor's `lock.json`, less the trials a resumed job already finished), and deleted once the last one has its fork. Shapes over about 26 GB of memory can't fork and always start fresh. When the budget is full, the parent needed farthest ahead in the plan makes room.
- Harbor runs every task once before any task twice, so without `TaskMajor` a task's parent waits through the whole job, and only the first few tasks fit the budget. `--plugin harbor_prized:TaskMajor` runs all attempts of a task together. It also deletes leftover parents at the end of the job.
- A refused fork (a full machine, a host being replaced) starts that trial fresh. A pull that fails fails the trial, as without fork mode. If the disk quota refuses a start, the parents are deleted and the rest of the run starts fresh, so fork mode never takes room a trial needs. On the free tier (100 GB of disk), keep `fork_disk_budget_gb` under what your running trials leave free.
- In [Docker mode](#dockerfile-and-compose-tasks) the parent is the sandbox right after the build and pulls, before any container starts, and it is shared by every task with the same environment (Harbor's `environment_id`: the same Dockerfile and files), the same template and shape. Each trial's fork writes its own Compose files and brings the project up without building. So Docker Hub sees one set of pulls per environment, and a run builds each environment once. A key used by two tasks is kept for the rest of the run, within `fork_disk_budget_gb`. If a fork is refused or its project doesn't come up, that trial builds fresh, with a warning saying so; after two such failures the environment builds fresh for the rest of the run.
- Parents are labelled `harbor-fork-parent=1` and `harbor-fork-run=<id>`, and they carry your `labels`. They are deleted at exit, even after Ctrl-C. One a killed process leaves behind is deleted after `paused_ttl_min` paused, or with `prized sandbox rm --label harbor-fork-parent=1`.

## Pull with your own registry credentials

Docker Hub allows 100 anonymous pulls an hour per IP address (its `ratelimit-limit` header, measured from a sandbox), and sandboxes share their host's address, so a full Terminal-Bench run can use it up, and starts then fail with `TOOMANYREQUESTS`. Pull with your own Docker Hub token instead; a free Personal account has a pull limit of its own that a full run can still reach, and paid plans don't:

```bash
export DOCKERHUB_USERNAME=you
export DOCKERHUB_TOKEN=dckr_pat_...   # Docker Hub: Account settings, Personal access tokens; "Public Repo Read-only", or "Read-only" for private images
harbor run -d terminal-bench/terminal-bench-2-1 -a oracle -e harbor_prized:PrizedEnvironment -n 4 --ek registry_auth=env
```

`registry_auth` names where the credentials are, never the credentials, because Harbor writes `--ek` values into the job's `config.json`:

| Value | Credentials |
|---|---|
| `env` | `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`, for images on Docker Hub |
| `docker` | Your `docker login`: `$DOCKER_CONFIG/config.json`, else `~/.docker/config.json`. Logins kept in a credential store (Docker Desktop's `credsStore`, `credHelpers`) are read with its `docker-credential-*` helper on your machine |
| a path | A Docker `config.json`, or a directory holding one, for any registry (also private images) |

Only the entry for the image's own registry goes to the sandbox, through the files API rather than a command line, and it is deleted as soon as the image is pulled, before the agent's first command. In Docker mode the entries for Docker Hub and for the registries the task's `docker_image`, Dockerfile `FROM` lines and Compose `image:` values name go into a Docker `config.json` that is `DOCKER_CONFIG` for the build and the pulls only; it is deleted, and checked gone, before any container starts or a fork parent pauses. A registry with no entry is pulled anonymously, with one warning a run. A credential store is asked once per registry every five minutes, not once a trial. Wrong credentials fail the pull (Docker Hub does not fall back to anonymous), and the error names where they came from.

## Results

Terminal-Bench 2.1, all 89 tasks, oracle agent, no retries, `-n 6`, November 3, 2026, with Harbor 0.25.0 from PyPI and this package (0.4.0): **80 of 89 pass (89.9%)** in 35 minutes, 2.79 sandbox-hours (about $0.38 at list price). Five of the failures are task rot that Sail also hit on Modal ([harbor#3475](https://github.com/harbor-framework/harbor/pull/3475#issuecomment-6022288555)); without them, 80 of 84 pass (95.2%).

| Fails | Why |
|---|---|
| build-cython-ext, mcmc-sampling-stan, protein-assembly, qemu-alpine-ssh, qemu-startup | The task itself: dependencies moved, Debian 404s, or the reference solution runs past its timeout. They fail on Modal too |
| configure-git-webserver, git-multibranch | `git push` over ssh: sshd's login needs a real switch to the git user |
| mailman | Postfix checks that `postdrop` is group-owned and setgid |
| torch-tensor-parallelism | gloo's teardown deadlocks on fewer than 4 vCPUs (the task asks for 1); it passes with `--ek min_vcpu=4` |

The same code built into Harbor main as `-e prized`, with the idle pause and heartbeat, also passed 80 of 89 that day, with the same nine failures. crack-7z-hash, the sixth task on Sail's list, passed in both runs: its reference solution's brute force took 24 minutes. On October 30 it ran past its timeout, and the same code, built into a Harbor branch, passed 79 of 89. 0.1.0 passed 70 of 89: ten tasks failed on `chown`, `dpkg` or `sudo`, and three on commands longer than about 15 minutes.

## Dockerfile and Compose tasks

A task whose environment is built from a Dockerfile (no `docker_image`, or a verifier image in `tests/Dockerfile`) or uses Docker Compose runs in Docker mode, by itself: its sandbox starts from the `prized-docker-v1` template (Docker Engine, Compose and Buildx; paid workspaces only), and the task runs under `docker compose` inside it, the way Harbor's own Docker provider runs it. The Dockerfile is built in the sandbox, sidecars start from the task's `docker-compose.yaml`, every command runs in the `main` service with `docker compose exec`, and files go in and out of it as root tar streams. Real users (an `sshd` login, Postfix) and setuid programs work. As on Harbor's Docker provider, the task's `cpus` and `memory_mb` cap the main service; the sandbox gets 512 MB of memory on top for the daemons. Tasks with a prebuilt `docker_image` and no Compose keep running without Docker, as above, unless a phase has no network: those run in Docker mode too, so Docker closes the network without waiting for Prized's network switch.

Network policies in Docker mode are enforced by Docker inside the sandbox. Images are built and pulled with the network. For a trial with a `no-network` phase (or an allowlist with no hosts), every network of the project is internal, so services reach each other by name and nothing reaches the internet; while a phase is public, an egress network is attached to the project's containers, and Harbor's switches between phases attach or detach it. The closed networks are also isolated (Docker 28's `gateway_mode` `isolated`), so containers can't reach services the sandbox itself listens on, and after every switch the plugin checks that each container's networks are as asked. A restricted trial whose Compose project uses host or bridge networking, an external network or another driver, a privileged container, `NET_ADMIN`-type capabilities, `pid: host`, the Docker socket, devices, or mounts from outside the task's folder (binds, local volumes' devices, file secrets and configs, symlinks out of it) is refused, before a sandbox is made where the task's YAML shows it. An allowlist that names hosts is refused, as are GPUs.

Where Prized refuses the template (not active yet, or a workspace on free credit alone), these tasks are refused as before, with the API's reason (a workspace on free credit gets the top-up link), and later trials of the run are refused before a sandbox is made, for 10 minutes. Without [`registry_auth`](#pull-with-your-own-registry-credentials), every Dockerfile's `FROM` and every sidecar image is pulled anonymously, and the sandboxes on one host share its address for Docker Hub's anonymous pull limit, so a large run can hit `toomanyrequests`. With `--ek fork_mode=true` each environment is built and pulled once and every trial forks it ([fork mode](#fork-mode-one-warm-start-per-task)).

Until then, or for GPU tasks, run them with Harbor's own `-e docker` on a [Prized box](https://prized.dev/docs?via=harbor) (a cloud VM with Docker), the way we ran Terminal-Bench 4.0 ([results](#terminal-bench-40)):

```bash
prized box new tb --size max --storage standard --ttl 6h --wait   # Large: 16 vCPU, 64 GB
prized ssh tb
# on the box:
curl -LsSf https://astral.sh/uv/install.sh | sh && . ~/.local/bin/env
uv tool install harbor
harbor run -d terminal-bench/terminal-bench@4.0.0 -a oracle -e docker -n 4
# back on your machine, once the run is done:
prized box delete tb -y
```

Keep `-n` low on one box: every trial pulls its images onto the same disk. One job at `-n 4` started all its 35 tasks; two jobs at once (up to 14 trials) pushed a start past Harbor's start timeout.

### Terminal-Bench 4.0

Terminal-Bench 4.0 (Hub `terminal-bench/terminal-bench@4.0.0`), oracle agent, October 31, 2026, run with Harbor 0.25.0's own `-e docker` on one Large Prized box (16 vCPU, 64 GB), the box route above: **62 of the 63 non-GPU tasks pass**, in about an hour of box time (about $0.75 at list price). The three GPU tasks need an H100 and were skipped.

| Task | Why |
|---|---|
| cad-model | Fails: the reference solution's unpinned pip dependencies moved (`ModuleNotFoundError: OCP.collections`) |
| shadow-relay, live-database-cutover, lake-temp-glm | Passed when rerun alone, after failing while two jobs ran up to 14 trials at once: a start timeout during image pulls, a 25 ms latency budget, and Harbor's egress-control probe failing under load (Harbor then refuses the no-network verifier for the rest of the job) |

Every task on the Hub ships prebuilt, digest-pinned images for its environment and its verifier. 11 of the 66 use Docker Compose (Docker mode) and 2 of the others give the verifier no network (Docker mode with `--ek docker=true`, or once Prized can switch a sandbox's network).

## Limits

- Without Docker mode: prebuilt `docker_image` tasks only, pulled anonymously unless you set [`registry_auth`](#pull-with-your-own-registry-credentials), and the image's `ENTRYPOINT` and `CMD` don't run. Dockerfile builds and Docker Compose tasks run in [Docker mode](#dockerfile-and-compose-tasks), or are refused before a sandbox starts where the workspace can't use its template (a verifier built from `tests/Dockerfile` is refused when the verifier starts, after the agent has run).
- One id underneath: a non-root user runs as its own uid and gid, but owns every file root owns, has no supplementary groups, can't `sudo`, and doesn't see other users' processes (root sees everyone's). Programs that check a real user switch (an `sshd` login, Postfix) fail.
- Anonymous pulls share a limit: Docker Hub allows 100 an hour per IP address (measured from a sandbox), and sandboxes share their host's address, so in a large run some starts can fail with `TOOMANYREQUESTS`. Pass your own token ([above](#pull-with-your-own-registry-credentials)), use `fork_mode` so each environment pulls once, or run those trials again later.
- Network policies without Docker mode (`--ek docker=false`): `no-network` (and `allow_internet = false`) and an allowlist with no hosts turn the sandbox's network off, enforced on the microVM by Prized, and Harbor may switch it between a trial's phases (setup with the network, the agent or the verifier without). The sandbox pulls the image with the network and switches before the task's first command. Where Prized can't switch a sandbox's network yet, the trial is refused with that reason (later trials of the run are refused before a sandbox is made, for 10 minutes). By default such tasks run in Docker mode. An allowlist that names hosts is refused, and so is a task that needs a GPU. Storage is at most 128 GB.
- Concurrency: each Terminal-Bench trial takes 1 vCPU, 2 GB of memory and 30 GB of disk, so the workspace's sandbox and disk quotas set how many run at once. Lower `image_headroom_gb` to fit more, or ask team@prized.dev to raise the quota for large runs. On the free tier pass `--ek max_runtime_min=60` (its limit) and `-n 1`: its 100 GB of disk fits three trial sandboxes, and a trial whose verifier runs in its own environment holds two while it verifies.

License: Apache-2.0.
