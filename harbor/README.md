# harbor-prized

Run [Harbor](https://github.com/harbor-framework/harbor) tasks, such as Terminal-Bench, in [Prized Sandboxes](https://prized.dev/docs/sandboxes?via=harbor) (Firecracker microVMs). No Harbor fork or PR needed.

```bash
uv tool install harbor --with "harbor-prized @ git+https://github.com/prized-dev/integrations#subdirectory=harbor"   # not on PyPI yet
export PRIZED_API_KEY=dcp_...   # https://prized.dev/onboarding/sandboxes?via=harbor
harbor run -d terminal-bench/terminal-bench-2-1 -a oracle -e harbor_prized:PrizedEnvironment -n 4
```

No Prized account yet? Your coding agent can sign you up with your email and a 6-digit code: have it follow https://prized.dev/auth.md?via=harbor.

Each trial gets its own sandbox, sized from the task's `cpus`, `memory_mb` and `storage_mb`, plus 20 GB of disk for the image, and deleted when the trial ends (`--no-delete` pauses it instead, at storage cost only, and it is deleted after a week paused). The task's prebuilt `docker_image` is unpacked with `crane`, keeping its file modes, and every command runs chrooted into it with the image's `ENV`, `WORKDIR` and `USER`, the task's env, Docker's umask (022) and a Docker-style `/etc/hosts`. Root commands run as uid 0 of an unprivileged user namespace, under a seccomp filter that makes `chown`, setuid/setgid bits and switches to other users succeed without effect, as fakeroot does, so `apt-get`, `useradd` and `sudo` work. A command for another user (`user=agent`, the task's `[agent] user`, or the image's `USER`) runs as that user's uid and gid in a namespace nested in root's. All commands of a trial share these namespaces, so processes they leave running (tmux, servers) stay visible to later commands. Long commands run detached through the [`prized` SDK](https://pypi.org/project/prized/), so an hour-long agent run outlives a dropped connection, and files over 64 MiB move in parts.

Options (`--ek key=value`): `docker` (`true` for [Docker mode](#docker-mode)), `template` (default `prized-base-v1`), `max_runtime_min` (1440; up to 10080, 7 days), `paused_ttl_min` (10080), `min_vcpu` (1), `image_headroom_gb` (20), `labels` (`k=v,k=v`). Each sandbox is labelled `harbor=1` and `harbor-session=<trial>`, so `prized sandbox rm --label harbor=1` removes any a crashed run left behind.

## Docker mode

`--ek docker=true` runs each trial's task under Docker inside its sandbox, the way Harbor's own Docker provider runs it: the sandbox starts from the `prized-docker-v1` template (root `dockerd` at boot, its socket open to the sandbox user), Harbor's compose files and the task's `environment/` go to `~/harbor`, and `docker compose` builds the task's Dockerfile or pulls its `docker_image`, starts any sidecars from the task's `docker-compose.yaml`, and every command runs in the `main` service with `docker compose exec`. So Dockerfile-only tasks, compose tasks, real users (an `sshd` login, Postfix) and setuid programs work. For `allow_internet = false` every network of the project is made internal: services still reach each other by name, and none of them reaches the internet (a task whose compose file uses `network_mode: host` or an external network is refused). As on Harbor's Docker provider, the task's `cpus` and `memory_mb` cap the main service; the sandbox gets 512 MB of memory on top for the daemons. Files go in and out of the main service as root tar streams, so they keep their modes and uploads are owned by root.

```bash
harbor run -d terminal-bench/terminal-bench-2-1 -a oracle -e harbor_prized:PrizedEnvironment --ek docker=true -n 4
```

`template=prized-docker-v1` turns Docker mode on by itself; `dockerd_wait_sec` (120) is how long a start waits for the daemon. Anonymous Docker Hub pulls are limited per IP address (100 an hour, measured from a sandbox), and the sandboxes on one host share its address, so a very large run can hit that limit.

## Results

Terminal-Bench 2.1, all 89 tasks, oracle agent, no retries, `-n 6`, October 30, 2026, with this code as the built-in `-e prized` of a Harbor branch (main c65b9f6d): **79 of 89 pass (88.8%)** in 46 minutes, 3.67 sandbox-hours (about $0.47 at list price). Six of the failures are task rot that Sail also hit on Modal ([harbor#3475](https://github.com/harbor-framework/harbor/pull/3475#issuecomment-6022288555)); without them, 79 of 83 pass (95.2%).

| Fails | Why |
|---|---|
| build-cython-ext, crack-7z-hash, mcmc-sampling-stan, protein-assembly, qemu-alpine-ssh, qemu-startup | The task itself: dependencies moved, Debian 404s, or the reference solution runs past its timeout. They fail on Modal too |
| configure-git-webserver, git-multibranch | `git push` over ssh: sshd's login needs a real switch to the git user |
| mailman | Postfix checks that `postdrop` is group-owned and setgid |
| torch-tensor-parallelism | gloo's teardown deadlocks on fewer than 4 vCPUs (the task asks for 1); it passes with `--ek min_vcpu=4` |

0.1.0 passed 70 of 89: ten tasks failed on `chown`, `dpkg` or `sudo`, and three on commands longer than about 15 minutes.

## Limits

Without Docker mode:

- Prebuilt `docker_image` tasks only, pulled without credentials. Dockerfile builds (including a verifier image built from `tests/Dockerfile`) and Docker Compose tasks are refused before a sandbox starts (use Docker mode). The image's `ENTRYPOINT` and `CMD` don't run.
- One id underneath: a non-root user runs as its own uid and gid, but owns every file root owns, has no supplementary groups, can't `sudo`, and doesn't see other users' processes (root sees everyone's). Programs that check a real user switch (an `sshd` login, Postfix) fail.
- No network policies (`no-network`, `allow_internet = false`, allowlists) or GPUs: Harbor refuses those tasks before a sandbox starts. Storage is at most 128 GB.
- Concurrency: each Terminal-Bench trial takes 1 vCPU, 2 GB of memory and 30 GB of disk, so the workspace's sandbox and disk quotas set how many run at once. Lower `image_headroom_gb` to fit more, or ask team@prized.dev to raise the quota for large runs. On the free tier pass `--ek max_runtime_min=60` (its limit); its 100 GB of disk fits three trials at a time.

License: Apache-2.0.
