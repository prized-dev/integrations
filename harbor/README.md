# harbor-prized

Run [Harbor](https://github.com/harbor-framework/harbor) tasks, such as Terminal-Bench, in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=harbor) (Firecracker microVMs). No Harbor fork or PR needed.

```bash
uv tool install harbor --with "harbor-prized @ git+https://github.com/prized-dev/integrations#subdirectory=harbor"   # harbor-prized is not on PyPI yet
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=harbor
harbor run -d terminal-bench/terminal-bench-2-1 -a oracle -e harbor_prized:PrizedEnvironment -n 4
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=harbor.

Each trial gets its own sandbox, sized from the task's `cpus`, `memory_mb` and `storage_mb`, plus 20 GB of disk for the image, and deleted when the trial ends (`--no-delete` pauses it instead, at storage cost only, and it is deleted after a day paused). If Harbor itself dies mid-run, a sandbox it leaves behind pauses after 30 idle minutes, so it stops billing compute, and is deleted a day later. Paused sandboxes count toward the workspace's disk quota until then. The task's prebuilt `docker_image` is unpacked with `crane`, keeping its file modes, and every command runs chrooted into it with the image's `ENV`, `WORKDIR` and `USER`, the task's env, Docker's umask (022) and a Docker-style `/etc/hosts`. Root commands run as uid 0 of an unprivileged user namespace, under a seccomp filter that makes `chown`, setuid/setgid bits and switches to other users succeed without effect, as fakeroot does, so `apt-get`, `useradd` and `sudo` work. A command for another user (`user=agent`, the task's `[agent] user`, or the image's `USER`) runs as that user's uid and gid in a namespace nested in root's. All commands of a trial share these namespaces, so processes they leave running (tmux, servers) stay visible to later commands. Long commands run detached through the [`prized` SDK](https://pypi.org/project/prized/), so an hour-long agent run outlives a dropped connection, and files over 64 MiB move in parts.

Options (`--ek key=value`): `template` (default `prized-base-v1`), `max_runtime_min` (1440; up to 10080, 7 days), `paused_ttl_min` (1440, a day), `min_vcpu` (1), `image_headroom_gb` (20), `labels` (`k=v,k=v`), `registry_auth` (`env`, `docker` or a path; [below](#pull-with-your-own-registry-credentials)). Each sandbox is labelled `harbor=1` and `harbor-session=<trial>`, so once no run is going, `prized sandbox rm --label harbor=1` removes any a crashed run left behind. A task's `cpus` may be fractional (`0.5`, Harbor 0.25 and later): the sandbox gets whole vCPUs, rounded up.

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

Only the entry for the image's own registry goes to the sandbox, through the files API rather than a command line, and it is deleted as soon as the image is pulled, before the agent's first command. A registry with no entry is pulled anonymously, with one warning a run. A credential store is asked once per registry every five minutes, not once a trial. Wrong credentials fail the pull (Docker Hub does not fall back to anonymous), and the error names where they came from.

## Results

Terminal-Bench 2.1, all 89 tasks, oracle agent, no retries, `-n 6`, November 3, 2026, with Harbor 0.25.0 from PyPI and this package (0.4.0): **80 of 89 pass (89.9%)** in 35 minutes, 2.79 sandbox-hours (about $0.38 at list price). Five of the failures are task rot that Sail also hit on Modal ([harbor#3475](https://github.com/harbor-framework/harbor/pull/3475#issuecomment-6022288555)); without them, 80 of 84 pass (95.2%).

| Fails | Why |
|---|---|
| build-cython-ext, mcmc-sampling-stan, protein-assembly, qemu-alpine-ssh, qemu-startup | The task itself: dependencies moved, Debian 404s, or the reference solution runs past its timeout. They fail on Modal too |
| configure-git-webserver, git-multibranch | `git push` over ssh: sshd's login needs a real switch to the git user |
| mailman | Postfix checks that `postdrop` is group-owned and setgid |
| torch-tensor-parallelism | gloo's teardown deadlocks on fewer than 4 vCPUs (the task asks for 1); it passes with `--ek min_vcpu=4` |

The same code built into Harbor main as `-e prized`, with the idle pause and heartbeat, also passed 80 of 89 that day, with the same nine failures. crack-7z-hash, the sixth task on Sail's list, passed in both runs: its reference solution's brute force took 24 minutes. On October 30 it ran past its timeout, and the same code, built into a Harbor branch, passed 79 of 89. 0.1.0 passed 70 of 89: ten tasks failed on `chown`, `dpkg` or `sudo`, and three on commands longer than about 15 minutes.

## Dockerfile and Compose tasks: use a Prized box

This environment runs a task's prebuilt image without Docker, so it refuses tasks that build a Dockerfile, use Docker Compose or give a step no network (see [Limits](#limits)). Run those with Harbor's own `-e docker` on a [Prized box](https://prized.dev/docs?via=harbor) (a cloud VM with Docker), the way we ran Terminal-Bench 4.0 ([results](#terminal-bench-40)):

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

Every task on the Hub ships prebuilt, digest-pinned images for its environment and its verifier. 11 of the 66 use Docker Compose and 2 of the others give the verifier no network: this environment can't run those 13, and the box route does.

## Limits

- Prebuilt `docker_image` tasks only, pulled anonymously unless you set [`registry_auth`](#pull-with-your-own-registry-credentials). Dockerfile builds and Docker Compose tasks are refused before a sandbox starts; a verifier built from `tests/Dockerfile` is refused when the verifier starts, after the agent has run (use the box route above). The image's `ENTRYPOINT` and `CMD` don't run.
- One id underneath: a non-root user runs as its own uid and gid, but owns every file root owns, has no supplementary groups, can't `sudo`, and doesn't see other users' processes (root sees everyone's). Programs that check a real user switch (an `sshd` login, Postfix) fail.
- Anonymous pulls share a limit: Docker Hub allows 100 an hour per IP address (measured from a sandbox), and sandboxes share their host's address, so in a large run some starts can fail with `TOOMANYREQUESTS`. Pass your own token ([above](#pull-with-your-own-registry-credentials)), or run those trials again later.
- No network policies (`no-network`, `allow_internet = false`, allowlists) or GPUs: Harbor refuses those tasks before a sandbox starts, and a verifier with no network of its own when it starts. Storage is at most 128 GB.
- Concurrency: each Terminal-Bench trial takes 1 vCPU, 2 GB of memory and 30 GB of disk, so the workspace's sandbox and disk quotas set how many run at once. Lower `image_headroom_gb` to fit more, or ask team@prized.dev to raise the quota for large runs. On the free tier pass `--ek max_runtime_min=60` (its limit) and `-n 1`: its 100 GB of disk fits three trial sandboxes, and a trial whose verifier runs in its own environment holds two while it verifies.

License: Apache-2.0.
